import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException

from config import AUTH_REQUIRED, SUPABASE_JWT_SECRET


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


def verify_supabase_jwt(token: str) -> Dict[str, Any]:
    if not SUPABASE_JWT_SECRET:
        raise auth_error("auth_not_configured", "Authentication is not configured on the server.")

    parts = token.split(".")
    if len(parts) != 3:
        raise auth_error("invalid_token", "Invalid authorization token.")

    encoded_header, encoded_payload, encoded_signature = parts
    try:
        header = json.loads(decode_base64url(encoded_header))
        claims = json.loads(decode_base64url(encoded_payload))
    except Exception:
        raise auth_error("invalid_token", "Invalid authorization token.")

    if header.get("alg") != "HS256":
        raise auth_error("unsupported_alg", "Unsupported authorization token algorithm.")

    signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected = hmac.new(SUPABASE_JWT_SECRET.encode("utf-8"), signed, hashlib.sha256).digest()
    actual = decode_base64url(encoded_signature)
    if not hmac.compare_digest(expected, actual):
        raise auth_error("invalid_signature", "Invalid authorization token signature.")

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
