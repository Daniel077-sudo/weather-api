import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives import hashes
from fastapi import Header, HTTPException

from config import AUTH_REQUIRED, SUPABASE_JWT_SECRET, SUPABASE_URL


JWKS_CACHE: Dict[str, Any] = {"fetched_at": 0, "keys": []}
JWKS_TTL_SECONDS = 3600


@dataclass
class AuthContext:
    user_id: str = ""
    claims: Optional[Dict[str, Any]] = None
    authenticated: bool = False


def auth_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "status": "error",
            "message": message,
            "source": "auth",
            "errors": [{"code": code, "message": message}],
        },
    )


def decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def decode_jwt_parts(token: str) -> tuple[Dict[str, Any], Dict[str, Any], str, str, str]:
    parts = token.split(".")
    if len(parts) != 3:
        raise auth_error("invalid_token", "Invalid authorization token.")

    encoded_header, encoded_payload, encoded_signature = parts
    try:
        header = json.loads(decode_base64url(encoded_header))
        claims = json.loads(decode_base64url(encoded_payload))
    except Exception:
        raise auth_error("invalid_token", "Invalid authorization token.")
    return header, claims, encoded_header, encoded_payload, encoded_signature


def validate_claims(claims: Dict[str, Any]) -> Dict[str, Any]:
    now = int(time.time())
    try:
        exp = claims.get("exp")
        if exp is not None and int(exp) < now:
            raise auth_error("token_expired", "Authorization token has expired.")
        nbf = claims.get("nbf")
        if nbf is not None and int(nbf) > now:
            raise auth_error("token_not_active", "Authorization token is not active yet.")
    except HTTPException:
        raise
    except Exception:
        raise auth_error("invalid_claims", "Authorization token contains invalid time claims.")
    sub = claims.get("sub")
    if not sub:
        raise auth_error("missing_sub", "Authorization token is missing subject.")
    return claims


def fetch_supabase_jwks(force_refresh: bool = False) -> list[Dict[str, Any]]:
    if not SUPABASE_URL:
        raise auth_error("auth_not_configured", "SUPABASE_URL is required for JWKS authentication.")

    now = int(time.time())
    cached_keys = JWKS_CACHE.get("keys") or []
    if cached_keys and not force_refresh and now - int(JWKS_CACHE.get("fetched_at") or 0) < JWKS_TTL_SECONDS:
        return cached_keys

    jwks_url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
    try:
        response = httpx.get(jwks_url, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        keys = payload.get("keys") or []
        if not isinstance(keys, list) or not keys:
            raise ValueError("JWKS response contains no keys")
        JWKS_CACHE["keys"] = keys
        JWKS_CACHE["fetched_at"] = now
        return keys
    except HTTPException:
        raise
    except Exception:
        raise auth_error("jwks_unavailable", "Unable to load Supabase JWKS.")


def find_jwk(kid: str) -> Optional[Dict[str, Any]]:
    for force_refresh in [False, True]:
        for key in fetch_supabase_jwks(force_refresh=force_refresh):
            if key.get("kid") == kid:
                return key
    return None


def verify_es256_jwt(header: Dict[str, Any], claims: Dict[str, Any], encoded_header: str, encoded_payload: str, encoded_signature: str) -> Dict[str, Any]:
    kid = header.get("kid")
    if not kid:
        raise auth_error("missing_kid", "Authorization token is missing key id.")

    jwk = find_jwk(str(kid))
    if not jwk:
        raise auth_error("unknown_kid", "Authorization token key id is not recognized.")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or not jwk.get("x") or not jwk.get("y"):
        raise auth_error("unsupported_key", "Authorization token key type is not supported.")

    try:
        x = int.from_bytes(decode_base64url(str(jwk["x"])), "big")
        y = int.from_bytes(decode_base64url(str(jwk["y"])), "big")
        public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()

        raw_signature = decode_base64url(encoded_signature)
        if len(raw_signature) != 64:
            raise ValueError("invalid ES256 signature length")
        r = int.from_bytes(raw_signature[:32], "big")
        s = int.from_bytes(raw_signature[32:], "big")
        der_signature = utils.encode_dss_signature(r, s)
        signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
        public_key.verify(der_signature, signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise auth_error("invalid_signature", "Invalid authorization token signature.")
    except HTTPException:
        raise
    except Exception:
        raise auth_error("invalid_signature", "Invalid authorization token signature.")
    return validate_claims(claims)


def verify_hs256_jwt(claims: Dict[str, Any], encoded_header: str, encoded_payload: str, encoded_signature: str) -> Dict[str, Any]:
    if not SUPABASE_JWT_SECRET:
        raise auth_error("auth_not_configured", "Authentication is not configured on the server.")

    signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected = hmac.new(SUPABASE_JWT_SECRET.encode("utf-8"), signed, hashlib.sha256).digest()
    actual = decode_base64url(encoded_signature)
    if not hmac.compare_digest(expected, actual):
        raise auth_error("invalid_signature", "Invalid authorization token signature.")
    return validate_claims(claims)


def verify_supabase_jwt(token: str) -> Dict[str, Any]:
    header, claims, encoded_header, encoded_payload, encoded_signature = decode_jwt_parts(token)
    alg = header.get("alg")
    if alg == "ES256":
        return verify_es256_jwt(header, claims, encoded_header, encoded_payload, encoded_signature)
    if alg == "HS256":
        return verify_hs256_jwt(claims, encoded_header, encoded_payload, encoded_signature)
    raise auth_error("unsupported_alg", "Unsupported authorization token algorithm.")


async def get_auth_context(authorization: Optional[str] = Header(None)) -> AuthContext:
    if not authorization:
        if AUTH_REQUIRED:
            raise auth_error("missing_token", "Authorization bearer token is required.")
        return AuthContext()

    prefix = "bearer "
    if not authorization.lower().startswith(prefix):
        raise auth_error("invalid_header", "Authorization header must use Bearer token format.")

    token = authorization[len("Bearer ") :].strip()
    claims = verify_supabase_jwt(token)
    return AuthContext(user_id=str(claims.get("sub") or ""), claims=claims, authenticated=True)


def resolve_user_id(auth: AuthContext, provided_user_id: Optional[str] = None) -> str:
    if auth.authenticated:
        return auth.user_id
    return str(provided_user_id or "")
