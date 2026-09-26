from __future__ import annotations

import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with library-recommended parameters

MIN_LENGTH = 12

# Verifying against this when the user doesn't exist keeps login timing uniform,
# so response times don't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def check_password_policy(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"password must be at least {MIN_LENGTH} characters")
    if password.strip() != password:
        raise ValueError("password must not start or end with whitespace")


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!@#%^*"
    return "".join(secrets.choice(alphabet) for _ in range(length))
