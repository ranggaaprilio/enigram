import hashlib
import hmac
import json

import pytest

from enigram.application.webhooks import parse_push, verify_webhook
from enigram.domain.errors import EnigramError

SECRET = "s" * 64
BODY = b'{"ref": "refs/heads/main"}'


def _github(body: bytes, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": f"sha256={digest}"}


def test_verify_webhook():
    assert verify_webhook(_github(BODY), BODY, SECRET)
    assert not verify_webhook(_github(BODY, "other"), BODY, SECRET)
    assert not verify_webhook(_github(BODY), BODY + b" ", SECRET)
    assert verify_webhook({"x-gitlab-token": SECRET}, BODY, SECRET)
    assert not verify_webhook({"x-gitlab-token": "nope"}, BODY, SECRET)
    assert not verify_webhook({}, BODY, SECRET)


def test_parse_github_push():
    body = {
        "ref": "refs/heads/main",
        "after": "a" * 40,
        "head_commit": {"timestamp": "2026-11-03T10:00:00+07:00"},
        "repository": {"pushed_at": 1793500000},
    }
    event = parse_push(json.dumps(body).encode())
    assert (event.ref_name, event.after, event.deleted, event.pushed_at) == (
        "main",
        "a" * 40,
        False,
        "2026-11-03T10:00:00+07:00",
    )
    del body["head_commit"]
    assert parse_push(json.dumps(body).encode()).pushed_at == "2026-11-01T02:26:40+00:00"


def test_parse_gitlab_push_and_tag():
    body = {
        "ref": "refs/tags/v1.2",
        "after": "b" * 40,
        "commits": [{"timestamp": "2026-11-03T03:00:00Z"}],
    }
    event = parse_push(json.dumps(body).encode())
    assert (event.ref_name, event.pushed_at) == ("v1.2", "2026-11-03T03:00:00Z")
    gitlab = {"ref": "refs/heads/dev", "after": None, "checkout_sha": "c" * 40}
    assert parse_push(json.dumps(gitlab).encode()).after == "c" * 40


def test_parse_branch_delete():
    body = {"ref": "refs/heads/old", "after": "0" * 40, "deleted": True}
    event = parse_push(json.dumps(body).encode())
    assert event.deleted and event.pushed_at is None


@pytest.mark.parametrize(
    "body", [b"not json", b"[1]", b'{"zen": "ping"}', b'{"ref": 1, "after": "a"}']
)
def test_parse_push_rejects_malformed(body):
    with pytest.raises(EnigramError):
        parse_push(body)
