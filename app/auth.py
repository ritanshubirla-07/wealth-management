import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dotenv import load_dotenv

load_dotenv()


COOKIE_NAME = "vikabh_session"
SESSION_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _secret() -> bytes:
    return os.getenv("AUTH_SECRET", "change-this-vikabh-session-secret").encode()


def create_session_token(claims: dict) -> str:
    payload = {**claims, "exp": int(time.time()) + SESSION_SECONDS}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(_secret(), encoded, hashlib.sha256).digest()
    return encoded.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest()
    try:
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if not hmac.compare_digest(expected, supplied) or payload.get("exp", 0) < time.time():
        return None
    return payload if payload.get("role") in {"admin", "client"} else None


def admin_credentials_match(username: str, password: str) -> bool:
    expected_username = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD", "VIKABH@Admin2026!")
    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(password, expected_password)