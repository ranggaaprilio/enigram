import pytest

from enigram.domain.secrets import REDACTED, is_secret_path, redact


@pytest.mark.parametrize(
    "text",
    [
        "const key = 'AKIAABCDEFGHIJKLMNOP';",
        "token: ghp_" + "a" * 36,
        # Split so the repository's private-key hook does not flag the test data itself.
        "-----BEGIN RSA PRIVATE " + "KEY-----\nMIIEow\n-----END RSA PRIVATE " + "KEY-----",
        "slack = 'xoxb-1234567890-abc'",
        "jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmn'",
        "const password = 'hunter2hunter2';",
        "AUTH_JWT_SECRET: 'abc123def456ghi'",
        "clientSecret = 'abc123def456ghi'",
        '"accessToken": "abc123def456ghi"',
    ],
)
def test_redact_each_pattern(text):
    out, n = redact(text)
    assert n == 1 and REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        "throw this.invalidLoginException({ password: 'incorrectPassword' });",
        "const token = 'refreshTokenExpired';",
        "password: process.env.DATABASE_PASSWORD,",
    ],
)
def test_redact_keeps_error_codes_and_env_reads(text):
    assert redact(text) == (text, 0)


def test_redact_counts_several_and_keeps_clean_code():
    clean = "async validateLogin(dto: Dto) {\n  return this.users.findByEmail(dto.email);\n}"
    assert redact(clean) == (clean, 0)
    out, n = redact("a = 'AKIAABCDEFGHIJKLMNOP'; b = 'AKIAQRSTUVWXYZ012345'")
    assert n == 2 and "AKIA" not in out


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env", True),
        (".env.local", True),
        ("config/id_rsa.pub", True),
        ("certs/server.pem", True),
        ("keys/service-account-prod.json", True),
        ("src/env.ts", False),
        ("src/config/app.config.ts", False),
        ("env-example-relational", False),
    ],
)
def test_is_secret_path(path, secret):
    assert is_secret_path(path) is secret
