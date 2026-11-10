"""Memory-hard password hashing and password strength policy using Python stdlib scrypt."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
from contextlib import contextmanager
from typing import Iterator, Tuple

# OWASP Password Storage Cheat Sheet minimum: N=2^17 (131072), r=8, p=1 (~128 MiB, ~754 ms on host)
DEFAULT_SCRYPT_N = 131072
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
MIN_SUPPORTED_SCRYPT_N = 16384
MAX_SUPPORTED_SCRYPT_N = 1048576
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16
SCRYPT_MAXMEM = 256 * 1024 * 1024

DEFAULT_MAX_CONCURRENT_SCRYPT = 4
DEFAULT_SCRYPT_ACQUIRE_TIMEOUT_SECONDS = 2.0


class ScryptConcurrencyLimitError(RuntimeError):
    """Raised when concurrent password hashing/verification slots are exhausted."""


class _BoundedScryptGate:
    """Bounds concurrent scrypt memory allocations to prevent RAM exhaustion under burst load."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limit = DEFAULT_MAX_CONCURRENT_SCRYPT
        self._active = 0
        self._cond = threading.Condition(self._lock)

    def _configured_limit(self) -> int:
        raw = os.getenv("SUBGUARD_MAX_CONCURRENT_SCRYPT", str(DEFAULT_MAX_CONCURRENT_SCRYPT)).strip()
        return max(1, min(16, int(raw)))

    def _acquire_timeout(self) -> float:
        raw = os.getenv("SUBGUARD_SCRYPT_ACQUIRE_TIMEOUT", str(DEFAULT_SCRYPT_ACQUIRE_TIMEOUT_SECONDS)).strip()
        return max(0.01, float(raw))

    @contextmanager
    def acquire(self) -> Iterator[None]:
        limit = self._configured_limit()
        timeout = self._acquire_timeout()
        with self._cond:
            deadline = None
            while self._active >= limit:
                if deadline is None:
                    import time
                    deadline = time.monotonic() + timeout
                else:
                    import time
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ScryptConcurrencyLimitError(
                            "Password verification service is busy; please retry shortly."
                        )
                    self._cond.wait(timeout=remaining)
                    continue
                self._cond.wait(timeout=timeout)
                limit = self._configured_limit()
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify()


_scrypt_gate = _BoundedScryptGate()

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
    with _scrypt_gate.acquire():
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
    if n < MIN_SUPPORTED_SCRYPT_N or n > MAX_SUPPORTED_SCRYPT_N or r < 1 or r > 16 or p < 1 or p > 4:
        raise ValueError("Password hash cost parameters outside safe bounds")
    salt = base64.urlsafe_b64decode(parts[4].encode("ascii"))
    expected = base64.urlsafe_b64decode(parts[5].encode("ascii"))
    return n, r, p, salt, expected


def needs_rehash(stored_hash: str) -> bool:
    """Return True if stored hash uses weaker parameters than current OWASP defaults."""
    try:
        n, r, p, _salt, _expected = _parse_scrypt_hash(stored_hash)
    except Exception:
        return False
    return n < DEFAULT_SCRYPT_N or r < DEFAULT_SCRYPT_R or p < DEFAULT_SCRYPT_P


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify plaintext password against stored scrypt hash using constant-time comparison."""
    if not password or not stored_hash:
        return False
    try:
        n, r, p, salt, expected = _parse_scrypt_hash(stored_hash)
    except Exception:
        return False

    with _scrypt_gate.acquire():
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


def dummy_password_verify(password: str) -> None:
    """Execute scrypt work matching real verification to prevent user enumeration by timing."""
    with _scrypt_gate.acquire():
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
