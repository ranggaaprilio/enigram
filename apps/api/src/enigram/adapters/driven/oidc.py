"""OpenID Connect login (plans/rbac.md, R4): the authorization code flow with PKCE against any
standard provider (Google, Microsoft Entra, Keycloak, ...), found through its discovery document.
The ID token comes straight from the token endpoint and is still checked in full: signature
(the provider's JWKS), issuer, audience, expiry and nonce."""

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from enigram.config import OidcConfig
from enigram.domain.access import Identity
from enigram.domain.errors import EnigramError

TIMEOUT_SECS = 15
# Asymmetric only: a symmetric algorithm would make the client secret a signing key.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
LEEWAY_SECS = 60  # clock skew allowed on exp, iat and nbf


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OidcProvider:
    def __init__(self, cfg: OidcConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.http = http  # tests pass one with a mock transport
        self._discovery: dict[str, Any] | None = None
        self._jwks: jwt.PyJWKSet | None = None

    async def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            if self.http is not None:
                return await self.http.request(method, url, timeout=TIMEOUT_SECS, **kw)
            async with httpx.AsyncClient() as http:
                return await http.request(method, url, timeout=TIMEOUT_SECS, **kw)
        except httpx.HTTPError as e:
            raise EnigramError(f"The login provider did not answer {url}: {e}") from e

    async def _json(self, url: str) -> dict[str, Any]:
        response = await self._send("GET", url)
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise EnigramError(f"The login provider answered {url} badly: {e}") from e

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            doc = await self._json(f"{self.cfg.issuer}/.well-known/openid-configuration")
            if str(doc.get("issuer", "")).rstrip("/") != self.cfg.issuer:
                raise EnigramError(
                    f"The provider calls itself {doc.get('issuer')!r}, not OIDC_ISSUER "
                    f"{self.cfg.issuer!r}"
                )
            self._discovery = doc
        return self._discovery

    async def authorize_url(self, state: str, nonce: str, verifier: str) -> str:
        doc = await self.discovery()
        params = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_url,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{doc['authorization_endpoint']}?{urlencode(params)}"

    async def _key(self, kid: str | None) -> Any:
        """The signing key named `kid`; the JWKS is fetched again once for a key it lacks (the
        provider rotated its keys)."""
        for refetch in (False, True):
            if self._jwks is None or refetch:
                doc = await self.discovery()
                self._jwks = jwt.PyJWKSet.from_dict(await self._json(doc["jwks_uri"]))
            keys = [k for k in self._jwks.keys if kid is None or k.key_id == kid]
            if keys:
                return keys[0].key
        raise EnigramError("The ID token is signed with a key the provider does not publish")

    async def identity(self, code: str, verifier: str, nonce: str) -> Identity:
        doc = await self.discovery()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.cfg.redirect_url,
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "code_verifier": verifier,
        }
        response = await self._send("POST", doc["token_endpoint"], data=form)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or "id_token" not in body:
            # The provider's error code only: its body may echo the code.
            raise EnigramError(f"The login provider refused the code ({body.get('error')})")
        token = body["id_token"]
        try:
            claims = jwt.decode(
                token,
                await self._key(jwt.get_unverified_header(token).get("kid")),
                algorithms=ALGORITHMS,
                audience=self.cfg.client_id,
                issuer=doc["issuer"],
                leeway=LEEWAY_SECS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            raise EnigramError(f"The ID token does not hold: {e}") from e
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise EnigramError("The ID token is not for this sign-in (nonce)")
        return identity_of(claims)


def identity_of(claims: dict[str, Any]) -> Identity:
    """The email of the claims: `email` (refused when the provider says it is unverified), else
    an email-shaped `preferred_username` (Microsoft Entra work accounts)."""
    if claims.get("email_verified") is False:
        raise EnigramError("The provider has not verified this account's email")
    email = str(claims.get("email") or "")
    if not email and "@" in str(claims.get("preferred_username", "")):
        email = str(claims["preferred_username"])
    if "@" not in email:
        raise EnigramError("The provider gave no email for this account (scope `email`)")
    name = claims.get("name")
    return Identity(email.strip().lower(), str(name).strip() if name else None)
