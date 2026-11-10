"""Explicit secure administrator bootstrap provisioning for SubGuard AI.

No default credentials are embedded in code or created automatically.
Admins must be provisioned explicitly either via:
1. CLI: python -m app.security.bootstrap create-admin --username ... --display-name ...
2. Environment variables: SUBGUARD_BOOTSTRAP_ADMIN_USERNAME & SUBGUARD_BOOTSTRAP_ADMIN_PASSWORD
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Optional

from app.persistence.database import init_db
from app.security.audit_log import log_security_event
from app.security.service import AuthService


def bootstrap_admin_from_env(db_path: Optional[Path] = None) -> Optional[dict]:
    """Create initial admin from explicit environment variables if configured and missing."""
    username = os.getenv("SUBGUARD_BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.getenv("SUBGUARD_BOOTSTRAP_ADMIN_PASSWORD", "")
    display_name = os.getenv("SUBGUARD_BOOTSTRAP_ADMIN_DISPLAY_NAME", "System Administrator").strip()

    if not username and not password:
        return None
    if not username or not password:
        raise RuntimeError(
            "Both SUBGUARD_BOOTSTRAP_ADMIN_USERNAME and SUBGUARD_BOOTSTRAP_ADMIN_PASSWORD must be set together."
        )

    init_db(db_path)
    svc = AuthService()
    existing = svc.get_user_by_username(username, db_path=db_path)
    if existing:
        return existing

    created = svc.create_user(
        username=username,
        display_name=display_name or "System Administrator",
        password=password,
        role="admin",
        is_active=True,
        enforce_password_policy=True,
        db_path=db_path,
    )
    log_security_event(
        "admin_bootstrapped",
        outcome="success",
        username=created["username"],
        user_id=created["id"],
        role=created["role"],
        details={"source": "environment"},
    )
    return created


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SubGuard AI Security Provisioning CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    admin_cmd = subparsers.add_parser("create-admin", help="Create an administrator account")
    admin_cmd.add_argument("--username", required=True, help="Administrator username")
    admin_cmd.add_argument("--display-name", default="System Administrator", help="Display name")
    admin_cmd.add_argument("--password-env", default=None, help="Env var holding initial password")
    admin_cmd.add_argument("--db-path", default=None, help="Optional explicit SQLite DB path")

    args = parser.parse_args(argv)
    if args.command == "create-admin":
        db_path = Path(args.db_path) if args.db_path else None
        init_db(db_path)
        if args.password_env:
            password = os.getenv(args.password_env, "")
            if not password:
                print(f"Error: environment variable {args.password_env!r} is empty or unset.", file=sys.stderr)
                return 1
        else:
            password = getpass.getpass("Enter administrator password (min 12 chars): ")
            confirm = getpass.getpass("Confirm administrator password: ")
            if password != confirm:
                print("Error: passwords do not match.", file=sys.stderr)
                return 1

        svc = AuthService()
        created = svc.create_user(
            username=args.username,
            display_name=args.display_name,
            password=password,
            role="admin",
            db_path=db_path,
        )
        print(f"Created admin user '{created['username']}' (id={created['id']}).")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
