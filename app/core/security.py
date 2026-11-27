"""Security primitives: hashing, envelope encryption, signed tokens.

Two distinct secret paths:
  api keys      hashed with sha256, only the hash is stored, plaintext shown once
  provider keys encrypted with Fernet so they can be decrypted for outbound calls
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.errors import AuthenticationError

API_KEY_PREFIX_LIVE = "sk_live_"
API_KEY_PREFIX_TEST = "sk_test_"


def generate_api_key(test: bool = False) -> tuple[str, str, str]:
    """Return (plaintext, hash, display_prefix).

    Plaintext is returned once and never stored. The display prefix lets a user
    recognise a key in a list without it being usable.
    """
    prefix = API_KEY_PREFIX_TEST if test else API_KEY_PREFIX_LIVE
    raw = secrets.token_urlsafe(32)
    plaintext = f"{prefix}{raw}"
    return plaintext, hash_api_key(plaintext), plaintext[: len(prefix) + 6]


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def verify_api_key(plaintext: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(plaintext), expected_hash)


def _fernet(encryption_key: str) -> Fernet:
    digest = hashlib.sha256(encryption_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str, encryption_key: str) -> str:
    return _fernet(encryption_key).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str, encryption_key: str) -> str:
    try:
        return _fernet(encryption_key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise AuthenticationError("could not decrypt stored secret") from exc


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_token(payload: dict[str, Any], secret: str, ttl_seconds: int) -> str:
    """Compact signed token: resume tickets, dashboard sessions, ingress keys.

    Deliberately not a full JWT library. Algorithm is fixed at HMAC-SHA256 so the
    alg-confusion and alg-none classes of attack are not expressible.
    """
    body = dict(payload)
    now = int(time.time())
    body["iat"] = now
    body["exp"] = now + ttl_seconds

    encoded = _b64url_encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64url_encode(signature)}"


def verify_token(token: str, secret: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 2:
        raise AuthenticationError("malformed token")

    encoded, provided_signature = parts
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_encode(expected), provided_signature):
        raise AuthenticationError("bad token signature")

    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(encoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("malformed token payload") from exc

    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthenticationError("token expired")
    return payload


_PASSWORD_ALGORITHM = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    """Self-contained password hash: no external dependency, no shared pepper file.

    The format carries its own algorithm name and iteration count so a future
    increase never invalidates passwords hashed under the old cost, and the salt
    travels with the hash rather than in a separate column.
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PASSWORD_ITERATIONS
    )
    return f"{_PASSWORD_ALGORITHM}${_PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected_hex = stored.split("$", 3)
    except (ValueError, AttributeError):
        return False
    if algorithm != _PASSWORD_ALGORITHM:
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected_hex)


def hash_identifier(value: str, salt: str) -> str:
    """Stable pseudonym for an external identifier such as a phone number.

    Salted per tenant so the same number under two tenants does not collide, which
    would otherwise leak that a person contacted both.
    """
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()


def constant_time_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
