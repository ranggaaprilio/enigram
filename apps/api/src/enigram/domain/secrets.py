"""Secrets in an indexed repository: files never to read, and values to redact from code before
it is stored or sent to a model."""

import fnmatch
import re

SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.jks",
    "credentials.json",
    "service-account*.json",
    ".npmrc",
    ".pypirc",
    ".netrc",
)
REDACTED = "[REDACTED]"
_PATTERNS = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
        ("github_token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
        (
            "private_key",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        ),
        ("slack_token", r"xox[baprs]-[0-9A-Za-z-]{10,}"),
        ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        # The value must mix letters and digits: `password: 'incorrectPassword'` is an error code
        # the answers need, not a secret.
        (
            "generic_assignment",
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
            r"['\"](?=[^'\"\s]*\d)(?=[^'\"\s]*[a-z])[^'\"\s]{12,}['\"]",
        ),
    )
)


def is_secret_path(rel: str) -> bool:
    """Whether the file's name (not its directory) marks it as holding secrets."""
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_FILE_PATTERNS)


def redact(text: str) -> tuple[str, int]:
    """The text with every secret-looking value replaced by [REDACTED], and how many there were."""
    total = 0
    for _, pattern in _PATTERNS:
        text, n = pattern.subn(REDACTED, text)
        total += n
    return text, total
