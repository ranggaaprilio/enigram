# ruff: noqa: F811 - fake_memory and keyed_env come from test_api as fixtures
"""Per-person login (plans/rbac.md, R4): the OIDC adapter against a provider faked with a real
RSA key, and the sign-in flow through the API with a fake provider."""

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_api import (  # noqa: F401 - fake_memory and keyed_env are fixtures
    LAPTOP_KEY,
    _bearer,
    _deps,
    fake_memory,
    keyed_env,
)
from test_query import FakeRag
from test_worker import FakeJobStore

from enigram.adapters.driven import lightrag_engine
from enigram.adapters.driven.oidc import OidcProvider, code_challenge, identity_of
from enigram.adapters.driving import http_api as api
from enigram.config import Config, OidcConfig
from enigram.domain.access import Identity
from enigram.domain.errors import EnigramError

ISSUER = "https://idp.example.test"
OIDC = OidcConfig(
    issuer=ISSUER,
    client_id="enigram",
    client_secret="secret-not-real",
    redirect_url="http://localhost:3000/api/auth/callback",
    allowed_domains=("example.com",),
    session_days=7,
)

# --- the adapter ---------------------------------------------------------------------------

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(kid="k1"):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**public, "kid": kid, "use": "sig", "alg": "RS256"}]}


def _id_token(**overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "enigram",
        "sub": "u-1",
        "iat": now,
        "exp": now + 300,
        "nonce": "n-1",
        "email": "Ann@Example.com",
        "email_verified": True,
        "name": "Ann Lee",
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


def _provider(token=None, *, issuer=ISSUER, token_status=200, jwks=None, seen=None):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append((request.method, url, request.content.decode()))
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if url.endswith("/jwks"):
            return httpx.Response(200, json=jwks or _jwks())
        if url.endswith("/token"):
            body = {"id_token": token} if token_status == 200 else {"error": "invalid_grant"}
            return httpx.Response(token_status, json=body)
        return httpx.Response(404)

    return OidcProvider(OIDC, httpx.AsyncClient(transport=httpx.MockTransport(handle)))


async def test_the_authorize_url_carries_state_nonce_and_the_pkce_challenge():
    url = urlparse(await _provider().authorize_url("s-1", "n-1", "v-1"))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}

    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    assert query["state"] == "s-1" and query["nonce"] == "n-1"
    assert query["code_challenge"] == code_challenge("v-1") != "v-1"
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == OIDC.redirect_url and query["scope"] == "openid email profile"


async def test_a_good_id_token_gives_the_identity_and_the_code_goes_with_the_verifier():
    seen = []
    who = await _provider(_id_token(), seen=seen).identity("c-1", "v-1", "n-1")

    assert who == Identity("ann@example.com", "Ann Lee")
    [(_, _, form)] = [s for s in seen if s[1].endswith("/token")]
    sent = {k: v[0] for k, v in parse_qs(form).items()}
    assert sent["code"] == "c-1" and sent["code_verifier"] == "v-1"


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (lambda: _id_token(nonce="other"), "nonce"),
        (lambda: _id_token(aud="someone-else"), "does not hold"),
        (lambda: _id_token(iss="https://evil.test"), "does not hold"),
        (lambda: _id_token(exp=int(time.time()) - 3600), "does not hold"),
        (lambda: _id_token(email_verified=False), "not verified"),
        (lambda: _id_token(email=None), "no email"),
        (lambda: jwt.encode({"iss": ISSUER}, "s" * 32, algorithm="HS256"), "hold"),
    ],
)
async def test_an_id_token_that_does_not_hold_is_refused(token, message):
    with pytest.raises(EnigramError, match=message):
        await _provider(token()).identity("c-1", "v-1", "n-1")


async def test_other_provider_failures_are_refused():
    with pytest.raises(EnigramError, match="calls itself"):
        await _provider(_id_token(), issuer="https://other.test").authorize_url("s", "n", "v")
    with pytest.raises(EnigramError, match="invalid_grant"):
        await _provider(token_status=400).identity("c-1", "v-1", "n-1")
    with pytest.raises(EnigramError, match="does not publish"):
        await _provider(_id_token(), jwks=_jwks("rotated")).identity("c-1", "v-1", "n-1")


def test_an_entra_work_account_without_email_uses_its_username():
    claims = {"preferred_username": "Bo@Example.com", "name": "Bo"}
    assert identity_of(claims) == Identity("bo@example.com", "Bo")


NOTE = {"target_kind": "entity", "target_ref": "Mira Okafor", "text": "Leads the study."}

# --- the flow through the API --------------------------------------------------------------


class FakeProvider:
    def __init__(self):
        self.who = Identity("ann@example.com", "Ann Lee")
        self.asked = []

    async def authorize_url(self, state, nonce, verifier):
        self.asked.append((state, nonce, verifier))
        return f"{ISSUER}/authorize?state={state}"

    async def identity(self, code, verifier, nonce):
        _, kept_nonce, kept_verifier = self.asked[-1]
        if (verifier, nonce) != (kept_verifier, kept_nonce):
            raise EnigramError("verifier or nonce lost")
        return self.who


@pytest.fixture
def login_env(pg_cfg, clean_env, fake_memory, monkeypatch):
    """App with OIDC and one API key; yields (client, store, provider)."""
    monkeypatch.setenv("ENIGRAM_API_KEYS", f"laptop-a:{LAPTOP_KEY}")
    for name, value in {
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": "enigram",
        "OIDC_CLIENT_SECRET": "secret-not-real",
        "OIDC_REDIRECT_URL": OIDC.redirect_url,
        "OIDC_ALLOWED_DOMAINS": "example.com",
    }.items():
        monkeypatch.setenv(name, value)
    cfg = Config.from_env(clean_env)
    store, provider = FakeJobStore(), FakeProvider()
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: FakeRag())
    deps = _deps(cfg, store, fake_memory)
    deps.identity_provider = lambda: provider
    with TestClient(api.create_app(cfg, deps)) as client:
        yield client, store, provider


def _sign_in(client, next_path="/w/default/code"):
    started = client.get("/auth/login", params={"next": next_path}, follow_redirects=False)
    assert started.status_code == 303, started.text
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    return client.get(
        "/auth/callback", params={"code": "c-1", "state": state}, follow_redirects=False
    )


def test_the_first_sign_in_makes_a_viewer_with_a_session(login_env):
    client, store, _ = login_env
    assert client.get("/auth/config").json() == {"login": True}
    assert client.get("/me").status_code == 401  # no cookie yet

    done = _sign_in(client)

    assert (done.status_code, done.headers["location"]) == (303, "/w/default/code")
    cookie = done.headers["set-cookie"]
    assert "enigram_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    me = client.get("/me").json()
    assert me["principal"]["name"] == "ann@example.com"
    assert (me["principal"]["kind"], me["principal"]["display_name"]) == ("user", "Ann Lee")
    assert me["roles"] == {"default": "viewer"}
    token = client.cookies["enigram_session"]
    assert token not in str(store.principals.sessions)  # only its hash is kept


def test_a_write_on_the_session_needs_the_header_and_takes_the_signed_in_name(login_env):
    client, store, _ = login_env
    _sign_in(client)
    pid = next(r["id"] for r in store.principals.rows.values() if r["name"] == "ann@example.com")
    store.principals.members[("default", pid)] = "contributor"

    bare = client.post("/annotations", json=NOTE)
    note = client.post(
        "/annotations",
        json={**NOTE, "by": "Someone Else"},
        headers={"X-Requested-With": "enigram"},
    )

    assert bare.status_code == 403 and "X-Requested-With" in bare.json()["detail"]
    assert note.status_code == 201, note.text
    assert (note.json()["author"], note.json()["by"]) == ("ann@example.com", "Ann Lee")


def test_sign_out_ends_the_session(login_env):
    client, _, _ = login_env
    _sign_in(client)
    token = client.cookies["enigram_session"]

    assert client.post("/auth/logout", headers={"X-Requested-With": "enigram"}).status_code == 204
    client.cookies.set("enigram_session", token)  # replayed after sign-out

    assert client.get("/me").status_code == 401


def test_a_key_still_works_beside_the_login(login_env):
    client, _, _ = login_env
    assert client.get("/me", headers=_bearer(LAPTOP_KEY)).json()["principal"]["name"] == "laptop-a"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s, p: setattr(p, "who", Identity("eve@other.com", "Eve")), "may not sign in"),
        (lambda s, p: setattr(p, "who", Identity("laptop-a", None)), "may not sign in"),
        (
            lambda s, p: s.principals.rows.update(
                {
                    99: {
                        "id": 99,
                        "name": "ann@example.com",
                        "kind": "user",
                        "key_hash": None,
                        "owner": False,
                        "default_workspace": "default",
                        "revoked": True,
                    }
                }
            ),
            "was revoked",
        ),
    ],
)
def test_a_refused_sign_in_goes_back_with_the_reason_and_no_session(login_env, change, message):
    client, store, provider = login_env
    change(store, provider)

    done = _sign_in(client)

    assert done.status_code == 303 and done.headers["location"].startswith("/?login_error=")
    assert message in parse_qs(urlparse(done.headers["location"]).query)["login_error"][0]
    assert "enigram_session" not in client.cookies


def test_a_callback_from_another_browser_is_refused(login_env):
    client, _, _ = login_env
    client.get("/auth/login", follow_redirects=False)

    forged = client.get(
        "/auth/callback", params={"code": "c", "state": "forged"}, follow_redirects=False
    )
    no_cookie = TestClient(client.app).get(
        "/auth/callback", params={"code": "c", "state": "x"}, follow_redirects=False
    )

    assert "does+not+match" in forged.headers["location"]
    assert "expired" in no_cookie.headers["location"].replace("+", " ")


@pytest.mark.parametrize("target", ["//evil.test/x", "https://evil.test", "/\\evil.test"])
def test_the_return_path_stays_on_this_site(login_env, target):
    client, _, _ = login_env
    assert _sign_in(client, target).headers["location"] == "/"


def test_the_login_cookie_holds_no_more_than_the_flow_needs(login_env):
    client, _, provider = login_env
    client.get("/auth/login", follow_redirects=False)
    kept = json.loads(base64.urlsafe_b64decode(client.cookies["enigram_login"]))
    assert set(kept) == {"state", "nonce", "verifier", "next"}
    assert (kept["state"], kept["nonce"], kept["verifier"]) == provider.asked[-1]


def _laptop_admin(store):
    pid = next(r["id"] for r in store.principals.rows.values() if r["name"] == "laptop-a")
    store.principals.members[("default", pid)] = "admin"
    return _bearer(LAPTOP_KEY)


def test_an_invited_person_signs_in_with_the_role_given(login_env):
    client, store, provider = login_env
    admin = _laptop_admin(store)

    invited = client.post(
        "/members", json={"email": " Bob@Example.com ", "role": "contributor"}, headers=admin
    )
    before = {m["name"]: m for m in client.get("/members", headers=admin).json()}
    provider.who = Identity("bob@example.com", "Bob")
    _sign_in(client)
    after = {m["name"]: m for m in client.get("/members", headers=admin).json()}

    assert invited.status_code == 201, invited.text
    assert invited.json() == {"name": "bob@example.com", "role": "contributor"}
    bob = before["bob@example.com"]
    assert (bob["kind"], bob["signed_in"]) == ("user", False)
    assert client.get("/me").json()["roles"] == {"default": "contributor"}  # not viewer
    assert after["bob@example.com"]["signed_in"] is True


def test_inviting_someone_already_signed_in_sets_the_role(login_env):
    client, store, _ = login_env
    admin = _laptop_admin(store)
    _sign_in(client)  # ann, viewer

    again = client.post(
        "/members", json={"email": "ann@example.com", "role": "maintainer"}, headers=admin
    )

    assert again.status_code == 201
    assert client.get("/me").json()["roles"] == {"default": "maintainer"}


@pytest.mark.parametrize(
    ("email", "message"),
    [
        ("eve@other.com", "may not sign in"),
        ("not-an-email", "an email"),
        ("laptop-a", "an email"),
    ],
)
def test_an_invite_needs_an_allowed_email(login_env, email, message):
    client, store, _ = login_env
    known = len(store.principals.rows)

    response = client.post(
        "/members", json={"email": email, "role": "viewer"}, headers=_laptop_admin(store)
    )

    assert response.status_code == 422 and message in response.json()["detail"]
    assert len(store.principals.rows) == known


def test_an_invite_refuses_a_key_name_and_a_revoked_person(login_env):
    client, store, _ = login_env
    admin = _laptop_admin(store)
    row = {"key_hash": None, "owner": False, "default_workspace": "default"}
    store.principals.rows[50] = {**row, "id": 50, "name": "bot@example.com", "kind": "key"}
    store.principals.rows[51] = {**row, "id": 51, "name": "old@example.com", "kind": "user"}
    store.principals.rows[50]["revoked"] = False
    store.principals.rows[51]["revoked"] = True

    key, gone = (
        client.post("/members", json={"email": email, "role": "viewer"}, headers=admin)
        for email in ("bot@example.com", "old@example.com")
    )

    assert key.status_code == 422 and "API key" in key.json()["detail"]
    assert gone.status_code == 422 and "revoked" in gone.json()["detail"]


def test_without_oidc_there_is_no_sign_in(keyed_env):
    client, _, _ = keyed_env
    assert client.get("/auth/config").json() == {"login": False}
    assert client.get("/auth/login", follow_redirects=False).status_code == 404


def test_without_oidc_nobody_is_invited(keyed_env):
    client, store, _ = keyed_env
    body = {"email": "bob@example.com", "role": "viewer"}
    response = client.post("/members", json=body, headers=_laptop_admin(store))
    assert response.status_code == 422 and "sign-in" in response.json()["detail"]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {"OIDC_CLIENT_ID": "x", "OIDC_CLIENT_SECRET": "y", "OIDC_REDIRECT_URL": "z"},
            "OIDC_ALLOWED_DOMAINS",
        ),
        ({"OIDC_ALLOWED_DOMAINS": "example.com"}, "OIDC_CLIENT_ID"),
    ],
)
def test_oidc_config_must_be_complete(cfg, clean_env, monkeypatch, env, message):
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(EnigramError, match=message):
        Config.from_env(clean_env)
