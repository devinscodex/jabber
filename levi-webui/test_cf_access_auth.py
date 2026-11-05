#!/usr/bin/env python3
"""Real, executable proof that cf_access_auth.py's verification actually
verifies -- not a mock of jwt.decode, a real RSA keypair, a real local
HTTP server standing in for Cloudflare's /cdn-cgi/access/certs JWKS
endpoint, and real jwt.encode()'d tokens signed with the real private
key. Every negative case below tampers with something real (wrong key,
wrong aud, expired timestamp, flipped signature byte, unknown kid) and
asserts CFAccessVerifier.verify() raises AuthError -- not "looks right,"
actually run (PROCEDURES CORE.md SS4: compute/verify with a tool, never
trust fluent-looking code).

Run: python3 test_cf_access_auth.py
"""
import base64
import http.server
import json
import sys
import threading
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from cf_access_auth import CFAccessVerifier, AuthError

TEAM_DOMAIN = "test-team"
AUD = "test-aud-tag-1234567890abcdef"
KID = "test-key-1"


def b64url_uint(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_numbers = priv.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": KID,
        "use": "sig",
        "alg": "RS256",
        "n": b64url_uint(pub_numbers.n),
        "e": b64url_uint(pub_numbers.e),
    }
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return priv_pem, jwk


def serve_jwks(jwks_body: bytes, port_holder: list):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(jwks_body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port_holder.append(srv.server_port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def main():
    priv_pem, jwk = make_keypair()
    jwks_body = json.dumps({"keys": [jwk]}).encode()
    port_holder = []
    srv = serve_jwks(jwks_body, port_holder)
    time.sleep(0.1)
    port = port_holder[0]
    certs_url = f"http://127.0.0.1:{port}/certs"
    issuer = "http://127.0.0.1:test-issuer"

    verifier = CFAccessVerifier(TEAM_DOMAIN, AUD, certs_url=certs_url, issuer=issuer)

    now = int(time.time())

    def token(aud=AUD, iss=issuer, exp=None, email="devin@example.com", key=priv_pem, kid=KID):
        payload = {
            "aud": aud,
            "iss": iss,
            "iat": now,
            "exp": exp if exp is not None else now + 3600,
            "email": email,
        }
        return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})

    results = []

    def check(name, fn):
        try:
            fn()
            results.append((name, "PASS"))
        except AssertionError as e:
            results.append((name, f"FAIL: {e}"))
        except Exception as e:
            results.append((name, f"FAIL (unexpected exception): {type(e).__name__}: {e}"))

    def valid_token_verifies():
        claims = verifier.verify(token())
        assert claims["email"] == "devin@example.com", claims

    def wrong_aud_rejected():
        try:
            verifier.verify(token(aud="some-other-application-aud"))
            assert False, "expected AuthError, got a claims dict"
        except AuthError:
            pass

    def wrong_issuer_rejected():
        try:
            verifier.verify(token(iss="http://127.0.0.1:not-the-real-issuer"))
            assert False, "expected AuthError, got a claims dict"
        except AuthError:
            pass

    def expired_rejected():
        try:
            verifier.verify(token(exp=now - 60))
            assert False, "expected AuthError, got a claims dict"
        except AuthError:
            pass

    def tampered_signature_rejected():
        t = token()
        # flip a char in the signature segment -- structurally still a
        # 3-part JWT, just cryptographically invalid.
        header, payload, sig = t.split(".")
        bad_char = "A" if sig[-1] != "A" else "B"
        tampered = f"{header}.{payload}.{sig[:-1]}{bad_char}"
        try:
            verifier.verify(tampered)
            assert False, "expected AuthError, got a claims dict"
        except AuthError:
            pass

    def wrong_key_rejected():
        other_priv_pem, _ = make_keypair()  # different keypair, same kid
        # sign with a DIFFERENT private key than the one JWKS actually
        # published under KID -- the whole point of signature verify.
        bad_token = token(key=other_priv_pem)
        try:
            verifier.verify(bad_token)
            assert False, "expected AuthError, got a claims dict"
        except AuthError:
            pass

    def unknown_kid_rejected():
        try:
            verifier.verify(token(kid="never-published-kid"))
            assert False, "expected AuthError, got a claims dict"
        except AuthError:
            pass

    def missing_email_rejected():
        payload = {"aud": AUD, "iss": issuer, "iat": now, "exp": now + 3600}
        bad_token = jwt.encode(payload, priv_pem, algorithm="RS256", headers={"kid": KID})
        try:
            verifier.verify(bad_token)
            assert False, "expected AuthError, got a claims dict"
        except AuthError:
            pass

    check("valid token verifies + email extracted", valid_token_verifies)
    check("wrong audience rejected", wrong_aud_rejected)
    check("wrong issuer rejected", wrong_issuer_rejected)
    check("expired token rejected", expired_rejected)
    check("tampered signature rejected", tampered_signature_rejected)
    check("signed with wrong private key rejected", wrong_key_rejected)
    check("unknown kid rejected", unknown_kid_rejected)
    check("missing email claim rejected", missing_email_rejected)

    srv.shutdown()

    print("=== cf_access_auth.py real verification test ===")
    all_pass = True
    for name, result in results:
        print(f"  [{result.split(':')[0]:4s}] {name}")
        if not result.startswith("PASS"):
            all_pass = False
            print(f"         {result}")

    if all_pass:
        print(f"\n{len(results)}/{len(results)} PASS")
        sys.exit(0)
    else:
        print("\nFAILED -- do not deploy")
        sys.exit(1)


if __name__ == "__main__":
    main()
