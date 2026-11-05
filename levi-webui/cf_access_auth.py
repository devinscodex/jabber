"""Cloudflare Access identity verification for Cairn WebUI.

Real server-side verification of the Cf-Access-Jwt-Assertion header that
Cloudflare Access injects on every request once it fronts this origin via
a Cloudflare Tunnel, per the 2026-08-12 council design (core/decision-
ledger.csv, personal/NOW.md "Beacon auth: Cloudflare Access + Entra ID"
PINNED block): Entra ID is the OIDC IdP behind Cloudflare Access; this
module is the "never trust header presence alone" half of that ruling --
it re-verifies the JWT's RS256 signature against Cloudflare's own
rotating public keys, plus aud/exp, before any claim in it is trusted.

Real dependency, not stdlib (2026-08-19 decision, Devin's explicit call):
PyJWT + cryptography. Hand-rolling RSA-PKCS1v15 verification for the
project's actual auth boundary was considered and rejected -- a subtle
padding/comparison bug there is a silent auth bypass, not a cosmetic bug,
and neither library needed a fresh install on either real deployment
target (both already present, Ouroboros system python3: cryptography
41.0.7, PyJWT 2.7.0).

Fails closed by construction: every real failure mode (bad signature,
wrong audience, expired, malformed token, JWKS fetch error, unknown kid)
raises AuthError. There is no code path that returns a claims dict
without a passing cryptographic verification having happened first.

Config (env vars, both required to activate enforcement):
    CF_ACCESS_TEAM_DOMAIN   e.g. "megnet" (your-team-name, NOT the full
                            .cloudflareaccess.com host -- this module
                            builds that URL itself)
    CF_ACCESS_AUD           the Application Audience (AUD) tag from the
                            Cloudflare Access Application's Overview tab

Unset (either or both): verification is DISABLED -- get_verifier()
returns None and callers fall back to today's single-user, no-auth
behavior. This is a deliberate escape hatch for local dev (`python3
server.py` with no tunnel in front of it), never a silent production
state -- server.py logs loudly at startup which mode it's in.
"""
import os
import json
import time
import urllib.request
import urllib.error

import jwt
from jwt import PyJWKClient


class AuthError(Exception):
    """Any verification failure. Callers must treat this as a hard 401 --
    never inspect .args and decide to proceed anyway."""


class CFAccessVerifier:
    def __init__(self, team_domain: str, aud: str, certs_url: str = None, issuer: str = None):
        # certs_url/issuer overrides exist ONLY for the self-test below
        # (a real local JWKS server standing in for Cloudflare's) --
        # get_verifier() never passes them, production always derives
        # both from team_domain.
        if not team_domain or not aud:
            raise ValueError("team_domain and aud are both required")
        self.team_domain = team_domain
        self.aud = aud
        self.certs_url = certs_url or f"https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs"
        self.issuer = issuer or f"https://{team_domain}.cloudflareaccess.com"
        self.identity_url = f"https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/get-identity"
        # PyJWKClient handles its own kid-keyed cache + re-fetch-on-miss
        # (a rotated key with an unseen kid triggers exactly one re-fetch,
        # not a fetch per request) -- real key rotation, not just startup.
        self._jwk_client = PyJWKClient(self.certs_url, cache_keys=True)
        # Real cache, 2026-10-08 (Devin: "are you able to grab the first
        # name instead of username?" -- a real pilot user's identity JWT
        # only guarantees "email", but Cloudflare's separate get-identity
        # endpoint (fed by whatever the Entra IdP actually passes through
        # in SSO) often carries a real human name). Keyed by the verified
        # email (not the raw token -- a token rotates far more often than
        # a person's name does), short TTL so a changed Entra profile name
        # eventually surfaces without needing a service restart, but a
        # normal multi-request session doesn't hit this endpoint on every
        # single page load/API call.
        self._identity_cache = {}  # email -> (name_or_None, fetched_at)
        self._identity_cache_ttl_s = 600

    def get_identity_name(self, token: str, email: str) -> str | None:
        """Best-effort real display name via CF Access's get-identity
        endpoint -- NOT part of the cryptographic verification path above
        (this is a convenience lookup, not a security boundary), so any
        failure here (network, non-200, malformed JSON, missing name
        field) returns None rather than raising -- /api/me's caller
        already has a real, safe fallback (the email-derived name) for
        exactly this case."""
        cached = self._identity_cache.get(email)
        if cached is not None:
            name, fetched_at = cached
            if time.time() - fetched_at < self._identity_cache_ttl_s:
                return name
        name = None
        try:
            req = urllib.request.Request(
                self.identity_url,
                headers={"Cookie": f"CF_Authorization={token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    identity = json.loads(resp.read())
                    raw_name = identity.get("name")
                    if isinstance(raw_name, str) and raw_name.strip():
                        name = raw_name.strip()
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            pass  # real best-effort -- the email-derived fallback covers this
        self._identity_cache[email] = (name, time.time())
        return name

    def verify(self, token: str) -> dict:
        """Return the decoded, VERIFIED claims dict, or raise AuthError.
        Never returns a claims dict for a token that failed any check."""
        if not token or not isinstance(token, str):
            raise AuthError("missing or non-string token")
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
        except (jwt.exceptions.PyJWKClientError, urllib.error.URLError) as exc:
            raise AuthError(f"could not resolve signing key: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.aud,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "aud", "email"]},
            )
        except jwt.exceptions.InvalidTokenError as exc:
            # Catches ExpiredSignatureError, InvalidAudienceError,
            # InvalidIssuerError, InvalidSignatureError, DecodeError, etc.
            # -- one net, deliberately: every one of these means "not a
            # verified request," none of them get different treatment.
            raise AuthError(f"token failed verification: {exc}") from exc

        if not claims.get("email"):
            # `require` above already enforces presence; this is a second,
            # explicit check on the ONE claim this module's callers
            # actually key identity on -- cheap insurance against a future
            # edit to `options` accidentally loosening the require list.
            raise AuthError("verified token has no email claim")

        return claims


_verifier = None
_verifier_checked = False


def get_verifier():
    """Lazy singleton. Returns None (auth disabled) if env vars are unset
    -- checked ONCE per process, not per request, so a mid-run env
    change never silently flips enforcement on/off without a restart."""
    global _verifier, _verifier_checked
    if _verifier_checked:
        return _verifier
    _verifier_checked = True
    team_domain = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip()
    aud = os.environ.get("CF_ACCESS_AUD", "").strip()
    if team_domain and aud:
        _verifier = CFAccessVerifier(team_domain, aud)
    return _verifier
