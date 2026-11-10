"""Memory-hard password hashing and password strength policy using Python stdlib scrypt."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Tuple

# Benchmarked on target host (~172 ms per derivation at 32 MiB working memory)
DEFAULT_SCRYPT_N = 32768
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16
SCRYPT_MAXMEM = 128 * 1024 * 1024

MIN_PASSWORD_LENGTH = 12
_BLOCKED_WEAK_PASSWORDS = {
    "password1234",
    "password12345",
    "admin1234567",
    "changeme1234",
    "subguard1234",
    "qwerty123456",
    "letmein12345",
}

# Precomputed valid dummy scrypt hash to normalize timing when a username does not exist.
_DUMMY_SALT = b"\x00" * SCRYPT_SALT_BYTES
_DUMMY_DERIVED = hashlib.scrypt(
    b"dummy-timing-normalization-input",
    salt=_DUMMY_SALT,
    n=DEFAULT_SCRYPT_N,
    r=DEFAULT_SCRYPT_R,
    p=DEFAULT_SCRYPT_P,
    maxmem=SCRYPT_MAXMEM,
    dklen=SCRYPT_DKLEN,
)


def validate_password_strength(password: str) -> None:
    """Enforce baseline password complexity for provisioned accounts."""
    if not isinstance(password, str) or not password or password.strip() != password:
        raise ValueError("Password must be non-empty without leading/trailing whitespace.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if password.lower() in _BLOCKED_WEAK_PASSWORDS:
        raise ValueError("Password is too common and cannot be used.")

    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        raise ValueError(
            "Password must contain at least 3 character classes (lowercase, uppercase, digits, symbols)."
        )


def hash_password(
    password: str,
    *,
    enforce_policy: bool = True,
    n: int = DEFAULT_SCRYPT_N,
    r: int = DEFAULT_SCRYPT_R,
    p: int = DEFAULT_SCRYPT_P,
) -> str:
    """Hash a password using parameterized hashlib.scrypt with random 128-bit salt."""
    if enforce_policy:
        validate_password_strength(password)

    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    dk_b64 = base64.urlsafe_b64encode(derived).decode("ascii")
    return f"scrypt${n}${r}${p}${salt_b64}${dk_b64}"


def _parse_scrypt_hash(stored_hash: str) -> Tuple[int, int, int, bytes, bytes]:
    parts = (stored_hash or "").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("Unsupported password hash format")
    n = int(parts[1])
    r = int(parts[2])
    p = int(parts[3])
    if n < 16384 or r < 1 or p < 1:
        raise ValueError("Password hash cost parameters below safe bounds")
    salt = base64.urlsafe_b64decode(parts[4].encode("ascii"))
    expected = base64.urlsafe_b64decode(parts[5].encode("ascii"))
    return n, r, p, salt, expected


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify plaintext password against stored scrypt hash using constant-time comparison."""
    if not password or not stored_hash:
        return False
    try:
        n, r, p, salt, expected = _parse_scrypt_hash(stored_hash)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
        return hmac.compare_digest(derived, expected)
    except Exception:
        return False


def dummy_password_verify(password: str) -> None:
    """Execute scrypt work matching real verification to prevent user enumeration by timing."""
    derived = hashlib.scrypt(
        (password or "").encode("utf-8"),
        salt=_DUMMY_SALT,
        n=DEFAULT_SCRYPT_N,
        r=DEFAULT_SCRYPT_R,
        p=DEFAULT_SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )
    hmac.compare_digest(derived, _DUMMY_DERIVED)


def hash_opaque_token(raw_token: str) -> str:
    """Return hex SHA-256 digest of a high-entropy CSPRNG session or API token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_opaque_token() -> str:
    """Generate a high-entropy URL-safe CSPRNG token (256 bits)."""
    return secrets.token_urlsafe(32)
