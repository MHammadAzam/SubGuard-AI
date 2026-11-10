"""Application security package for SubGuard AI (Phase 2 / ISS-02).

Provides:
- Fail-closed production security configuration validation
- Memory-hard password hashing via Python stdlib hashlib.scrypt
- RBAC role definitions and enforcement dependencies
- Server-side hashed session cookies and opaque revocable Bearer API tokens
- Synchronizer-token CSRF protection
- Thread-safe per-process login and mutation rate limiting
- Structured security audit logging without secret exposure
- Explicit administrator bootstrap provisioning (no default credentials)
"""
