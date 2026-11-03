"""Push webhooks from GitHub and GitLab: signature check and the push fields an index job needs.
Pure functions; the route lives in http_api.py."""

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from enigram.domain.errors import EnigramError

ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class PushEvent:
    ref_name: str  # branch or tag name, refs/heads/ or refs/tags/ stripped
    after: str  # the pushed head
    deleted: bool  # a branch or tag deletion: nothing to index
    pushed_at: str | None  # ISO 8601, when the payload says


def verify_webhook(headers: Mapping[str, str], body: bytes, secret: str) -> bool:
    """GitHub: `X-Hub-Signature-256: sha256=<HMAC-SHA256(secret, body)>`; GitLab: `X-Gitlab-Token`
    equals the secret. Header names are lower case (Starlette); neither header is a mismatch."""
    github = headers.get("x-hub-signature-256")
    if github is not None:
        expected = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
        return hmac.compare_digest(github.encode(), expected.encode())
    gitlab = headers.get("x-gitlab-token")
    if gitlab is not None:
        return hmac.compare_digest(gitlab.encode(), secret.encode())
    return False


def _pushed_at(payload: dict[str, Any]) -> str | None:
    head = payload.get("head_commit")
    if isinstance(head, dict) and isinstance(head.get("timestamp"), str):
        return head["timestamp"]
    repo = payload.get("repository")
    pushed = repo.get("pushed_at") if isinstance(repo, dict) else None
    if isinstance(pushed, int | float) and not isinstance(pushed, bool):
        return datetime.fromtimestamp(pushed, UTC).isoformat()
    commits = payload.get("commits")
    if isinstance(commits, list) and commits and isinstance(commits[-1], dict):
        stamp = commits[-1].get("timestamp")
        return stamp if isinstance(stamp, str) else None
    return None


def parse_push(body: bytes) -> PushEvent:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise EnigramError("Webhook body is not JSON") from e
    if not isinstance(payload, dict):
        raise EnigramError("Webhook body is not a JSON object")
    ref = payload.get("ref")
    after = payload.get("after") or payload.get("checkout_sha")
    if not isinstance(ref, str) or not isinstance(after, str):
        raise EnigramError("Not a push event: no ref or after")
    name = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    deleted = after == ZERO_SHA or payload.get("deleted") is True
    return PushEvent(name, after, deleted, _pushed_at(payload))
