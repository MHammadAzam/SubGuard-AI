"""Operational backup, integrity verification, and staged restore utilities for SubGuard AI.

Uses SQLite's Online Backup API (`sqlite3.Connection.backup()`) paired with
manifest checksums for uploaded PDFs. Never copies raw running `-wal` files directly,
never includes secrets/env files, and never silently deletes user documents or overwrites
active application data directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import posixpath
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.documents.review_service import get_upload_dir
from app.persistence.database import get_db_path, get_schema_version

_EXCLUDED_BACKUP_PATTERNS = (".env", ".pem", ".key", "-wal", "-shm")


class BackupError(RuntimeError):
    """Raised when backup, verification, or restore fails safety checks."""


def _validate_safe_single_component(value: str, *, field_name: str) -> str:
    """Reject empty, traversal, NUL, drive-prefixed, or multi-segment path components."""
    if not isinstance(value, str):
        raise BackupError(f"Invalid {field_name}: expected string.")
    if not value or "\x00" in value or value in {".", ".."}:
        raise BackupError(f"Unsafe {field_name}: {value!r}")
    if "/" in value or "\\" in value or ntpath.splitdrive(value)[0]:
        raise BackupError(f"Path separator or drive prefix not permitted in {field_name}: {value!r}")
    return value


def _extract_safe_doc_rel_parts(submission_id: str, raw_storage_path: str) -> tuple[str, str]:
    """Validate submission_id and raw DB storage_path and return safe (submission_id, filename)."""
    safe_sub_id = _validate_safe_single_component(submission_id, field_name="submission_id")
    if not isinstance(raw_storage_path, str) or not raw_storage_path.strip() or "\x00" in raw_storage_path:
        raise BackupError(f"Unsafe documents.storage_path value: {raw_storage_path!r}")

    win_parts = PureWindowsPath(raw_storage_path).parts
    posix_parts = PurePosixPath(raw_storage_path).parts
    if ".." in win_parts or ".." in posix_parts:
        raise BackupError(f"Path traversal ('..') detected in documents.storage_path: {raw_storage_path!r}")

    # Support paths recorded on either Windows or POSIX hosts while enforcing submission folder confinement
    normalized_slash = raw_storage_path.replace("\\", "/").rstrip("/")
    parent_dir = posixpath.basename(posixpath.dirname(normalized_slash))
    filename = posixpath.basename(normalized_slash)
    safe_filename = _validate_safe_single_component(filename, field_name="document filename")
    if parent_dir != safe_sub_id:
        raise BackupError(
            f"documents.storage_path {raw_storage_path!r} does not belong to submission directory {safe_sub_id!r}."
        )
    return safe_sub_id, safe_filename


def _resolve_manifest_entry_path(
    rel_path: str,
    *,
    target_db: Path,
    target_uploads: Path,
) -> Path:
    """Strictly resolve a manifest 'files' key inside target_uploads or exact DB target."""
    if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path or "\\" in rel_path:
        raise BackupError(f"Invalid manifest entry path: {rel_path!r}")
    if rel_path == "database/subguard.db":
        if target_db.is_symlink():
            raise BackupError(f"Target database path must not be a symlink: '{target_db}'")
        return target_db
    if not rel_path.startswith("uploads/"):
        raise BackupError(f"Unexpected manifest entry outside database/ or uploads/: {rel_path!r}")

    sub_rel = rel_path[len("uploads/"):]
    posix_rel = PurePosixPath(sub_rel)
    if (
        not sub_rel
        or posix_rel.is_absolute()
        or ".." in posix_rel.parts
        or "." in posix_rel.parts
        or ntpath.splitdrive(sub_rel)[0]
    ):
        raise BackupError(f"Unsafe manifest upload path: {rel_path!r}")

    resolved_root = target_uploads.resolve()
    current = target_uploads
    for part in posix_rel.parts:
        _validate_safe_single_component(part, field_name="manifest path segment")
        current = current / part
        if current.is_symlink():
            raise BackupError(f"Symlink detected in backup/target upload path: '{current}'")
    resolved_candidate = current.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise BackupError(f"Manifest path escapes upload directory: {rel_path!r}")
    return current


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_excluded_asset(path: Path) -> bool:
    name_lower = path.name.lower()
    if name_lower.startswith(".env"):
        return True
    return any(name_lower.endswith(suffix) for suffix in _EXCLUDED_BACKUP_PATTERNS)


def _sqlite_integrity_and_fk_check(db_path: Path) -> tuple[str, List[Dict[str, Any]]]:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check;").fetchone()[0])
        fk_rows = conn.execute("PRAGMA foreign_key_check;").fetchall()
        fk_violations = [
            {"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]} for r in fk_rows
        ]
        return integrity, fk_violations
    finally:
        conn.close()


def _verify_db_document_references(
    db_path: Path,
    upload_root: Path,
    *,
    expected_storage_root: Optional[Path] = None,
) -> tuple[List[Dict[str, str]], List[str]]:
    """Verify every processed document DB record safely resolves under upload_root, and list unreferenced files."""
    if db_path.is_symlink():
        raise BackupError(f"Database path must not be a symlink: '{db_path}'")
    if upload_root.exists() and upload_root.is_symlink():
        raise BackupError(f"Upload directory must not be a symlink: '{upload_root}'")

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, submission_id, storage_path, processing_status FROM documents"
        ).fetchall()
    finally:
        conn.close()

    resolved_root = upload_root.resolve()
    resolved_expected_root = expected_storage_root.resolve() if expected_storage_root is not None else None
    referenced_rel_paths = set()
    missing_files: List[Dict[str, str]] = []

    for row in rows:
        raw_sp = (row["storage_path"] or "").strip()
        if not raw_sp:
            continue
        safe_sub_id, safe_fname = _extract_safe_doc_rel_parts(row["submission_id"], raw_sp)

        if resolved_expected_root is not None:
            expected_abs = (resolved_expected_root / safe_sub_id / safe_fname).resolve()
            sp_path = Path(raw_sp)
            if not sp_path.is_absolute():
                raise BackupError(
                    f"Expected absolute documents.storage_path under upload root, got relative path: {raw_sp!r}"
                )
            if sp_path.is_symlink():
                raise BackupError(f"Document storage_path points to a symlink: {raw_sp!r}")
            if sp_path.resolve() != expected_abs:
                raise BackupError(
                    f"documents.storage_path {raw_sp!r} does not match expected target '{expected_abs}'."
                )

        sub_dir = upload_root / safe_sub_id
        candidate_file = sub_dir / safe_fname
        if sub_dir.is_symlink() or candidate_file.is_symlink():
            raise BackupError(f"Symlink detected for document '{candidate_file}'")

        target = candidate_file.resolve()
        rel_candidate = Path(safe_sub_id) / safe_fname
        if not target.is_relative_to(resolved_root) or not target.exists() or not target.is_file():
            missing_files.append(
                {
                    "document_id": row["id"],
                    "submission_id": safe_sub_id,
                    "expected_relative_path": rel_candidate.as_posix(),
                }
            )
        else:
            referenced_rel_paths.add(target.relative_to(resolved_root).as_posix())

    orphaned_files: List[str] = []
    if resolved_root.exists():
        for root_dir, _dirs, files in os.walk(resolved_root, followlinks=False):
            root_path = Path(root_dir)
            if root_path.is_symlink():
                raise BackupError(f"Symlink directory detected inside uploads: '{root_path}'")
            for fname in files:
                fpath = root_path / fname
                if fpath.is_symlink():
                    raise BackupError(f"Symlink file detected inside uploads: '{fpath}'")
                resolved_file = fpath.resolve()
                if not resolved_file.is_relative_to(resolved_root):
                    raise BackupError(f"File resolves outside upload directory: '{fpath}'")
                rel = resolved_file.relative_to(resolved_root).as_posix()
                if rel not in referenced_rel_paths:
                    orphaned_files.append(rel)

    return missing_files, sorted(orphaned_files)


def backup_application_data(
    output_dir: Path,
    *,
    db_path: Optional[Path] = None,
    upload_dir: Optional[Path] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a consistent SQLite Online Backup + upload bundle inside output_dir.

    Consistency note: SQLite snapshot uses sqlite3.Connection.backup() which is safe alongside
    WAL readers/writers, whereas filesystem upload files are copied afterwards. Pause uploads
    during backup for strict DB-to-upload point-in-time equivalence; any concurrent unreferenced
    files discovered in upload_dir are flagged in the manifest without deleting them.
    """
    raw_db = db_path or get_db_path()
    raw_uploads = upload_dir or get_upload_dir()
    if raw_db.is_symlink():
        raise BackupError(f"Refusing to back up symlinked database path: '{raw_db}'")
    if raw_uploads.exists() and raw_uploads.is_symlink():
        raise BackupError(f"Refusing to back up symlinked upload directory: '{raw_uploads}'")

    src_db = raw_db.resolve()
    src_uploads = raw_uploads.resolve()
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if not src_db.exists() or not src_db.is_file():
        raise BackupError(f"Source SQLite database does not exist: '{src_db}'")

    backup_name = (
        name
        or f"subguard_backup_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:6]}"
    )
    _validate_safe_single_component(backup_name, field_name="backup name")
    final_dir = out_root / backup_name
    if not final_dir.resolve().is_relative_to(out_root):
        raise BackupError(f"Invalid backup destination outside output_dir: '{final_dir}'")
    if final_dir.exists():
        raise BackupError(
            f"Refusing to overwrite existing backup directory '{final_dir}'."
        )

    staging_dir = out_root / f".staging_{backup_name}_{uuid4().hex[:6]}"
    staging_db_dir = staging_dir / "database"
    staging_uploads_dir = staging_dir / "uploads"
    staging_db_dir.mkdir(parents=True, exist_ok=True)
    staging_uploads_dir.mkdir(parents=True, exist_ok=True)

    staged_db = staging_db_dir / "subguard.db"

    try:
        # 1. Online SQLite Backup API snapshot using RFC-3986 file URI encoding (?/# spaces safe)
        src_conn = sqlite3.connect(f"{src_db.as_uri()}?mode=ro", uri=True, timeout=30.0)
        dst_conn = sqlite3.connect(str(staged_db), timeout=30.0)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # 2. Verify SQLite integrity & FKs on snapshot
        integrity, fk_violations = _sqlite_integrity_and_fk_check(staged_db)
        if integrity.lower() != "ok":
            raise BackupError(f"Staged DB integrity_check failed: {integrity}")
        if fk_violations:
            raise BackupError(f"Staged DB foreign_key_check failed: {fk_violations}")

        # 3. Copy uploads into staging (excluding secret/wal artifacts & symlinks)
        files_manifest: Dict[str, Dict[str, Any]] = {}
        if src_uploads.exists():
            for root_dir, dirnames, files in os.walk(src_uploads, followlinks=False):
                # Do not traverse symlinked subdirectories
                dirnames[:] = [d for d in dirnames if not (Path(root_dir) / d).is_symlink()]
                rel_root = Path(root_dir).resolve().relative_to(src_uploads)
                for fname in files:
                    src_file = Path(root_dir) / fname
                    if src_file.is_symlink() or _is_excluded_asset(src_file):
                        continue
                    dest_file = staging_uploads_dir / rel_root / fname
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest_file)
                    rel_key = (Path("uploads") / rel_root / fname).as_posix()
                    files_manifest[rel_key] = {
                        "size_bytes": dest_file.stat().st_size,
                        "sha256": _sha256_file(dest_file),
                    }

        files_manifest["database/subguard.db"] = {
            "size_bytes": staged_db.stat().st_size,
            "sha256": _sha256_file(staged_db),
        }

        # 4. Cross-check DB document references (strict containment + live root match) against staged uploads
        missing_refs, orphaned = _verify_db_document_references(
            staged_db,
            staging_uploads_dir,
            expected_storage_root=src_uploads if src_uploads.exists() else None,
        )
        if missing_refs:
            raise BackupError(
                f"Backup aborted: DB references {len(missing_refs)} missing upload file(s): {missing_refs[:5]}"
            )

        manifest = {
            "format_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": get_schema_version(staged_db),
            "file_count": len(files_manifest),
            "integrity_check": integrity,
            "snapshot_consistency": {
                "sqlite_backup_api": "online",
                "quiet_window_recommended": True,
                "unreferenced_file_count": len(orphaned),
            },
            "orphaned_files": orphaned,
            "files": files_manifest,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )

        # 5. Atomic publish only after all checks succeeded
        os.replace(staging_dir, final_dir)
        return {
            "status": "ok",
            "backup_dir": str(final_dir),
            "file_count": len(files_manifest),
            "orphaned_files": orphaned,
            "integrity_check": integrity,
        }
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _verify_manifest_entries(
    manifest_files: Any,
    *,
    target_db: Optional[Path],
    target_uploads: Path,
    verify_db_hash: bool = True,
) -> List[Dict[str, str]]:
    """Validate manifest path confinement and verify SHA-256 digests."""
    if not isinstance(manifest_files, dict):
        raise BackupError("Invalid backup manifest: 'files' must be a JSON object.")

    hash_mismatches: List[Dict[str, str]] = []
    for rel_path, meta in manifest_files.items():
        if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str):
            raise BackupError(f"Invalid manifest metadata for entry: {rel_path!r}")
        if rel_path == "database/subguard.db" and not verify_db_hash:
            continue
        candidate = _resolve_manifest_entry_path(
            rel_path,
            target_db=target_db or (target_uploads.parent / "database" / "subguard.db"),
            target_uploads=target_uploads,
        )
        if not candidate.exists() or not candidate.is_file():
            hash_mismatches.append({"path": rel_path, "reason": "missing_on_disk"})
        else:
            actual = _sha256_file(candidate)
            if actual != meta["sha256"]:
                hash_mismatches.append({"path": rel_path, "reason": "sha256_mismatch"})
    return hash_mismatches


def verify_data_integrity(
    *,
    db_path: Optional[Path] = None,
    upload_dir: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    expected_storage_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify SQLite integrity, FKs, referenced documents on disk, and optional manifest hashes."""
    raw_db = db_path or get_db_path()
    raw_uploads = upload_dir or get_upload_dir()
    if raw_db.is_symlink():
        raise BackupError(f"Database path must not be a symlink: '{raw_db}'")
    if raw_uploads.exists() and raw_uploads.is_symlink():
        raise BackupError(f"Upload directory must not be a symlink: '{raw_uploads}'")

    target_db = raw_db.resolve()
    target_uploads = raw_uploads.resolve()

    if not target_db.exists():
        return {
            "ok": False,
            "error": f"Database file not found: '{target_db}'",
            "missing_files": [],
            "orphaned_files": [],
            "hash_mismatches": [],
            "foreign_key_violations": [],
        }

    integrity, fk_violations = _sqlite_integrity_and_fk_check(target_db)
    missing_refs, orphaned = _verify_db_document_references(
        target_db,
        target_uploads,
        expected_storage_root=expected_storage_root,
    )

    hash_mismatches: List[Dict[str, str]] = []
    if manifest_path is not None:
        manifest_data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        manifest_files = manifest_data.get("files")
        hash_mismatches = _verify_manifest_entries(
            manifest_files,
            target_db=target_db,
            target_uploads=target_uploads,
            verify_db_hash=True,
        )

    ok = (
        integrity.lower() == "ok"
        and len(fk_violations) == 0
        and len(missing_refs) == 0
        and len(hash_mismatches) == 0
    )
    return {
        "ok": ok,
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "missing_files": missing_refs,
        "orphaned_files": orphaned,
        "hash_mismatches": hash_mismatches,
        "schema_version": get_schema_version(target_db),
    }


def _copy_verified_backup_to_staging(
    *,
    backup_db: Path,
    backup_uploads: Path,
    staging_db: Path,
    staging_uploads: Path,
    manifest_files: Dict[str, Any],
) -> None:
    """Copy manifest-validated files (never following symlinks) into staging and re-verify hashes."""
    if "database/subguard.db" not in manifest_files:
        raise BackupError("Backup manifest missing required 'database/subguard.db' entry.")

    staging_uploads.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_db, staging_db)
    db_expected_hash = manifest_files["database/subguard.db"]["sha256"]
    if _sha256_file(staging_db) != db_expected_hash:
        raise BackupError("Staged database SHA-256 verification failed after copy.")

    for rel_path, meta in manifest_files.items():
        if rel_path == "database/subguard.db":
            continue
        src_file = _resolve_manifest_entry_path(
            rel_path,
            target_db=backup_db,
            target_uploads=backup_uploads,
        )
        dst_file = _resolve_manifest_entry_path(
            rel_path,
            target_db=staging_db,
            target_uploads=staging_uploads,
        )
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        if _sha256_file(dst_file) != meta["sha256"]:
            raise BackupError(f"Staged upload SHA-256 verification failed for '{rel_path}'.")


def restore_application_data(
    backup_dir: Path,
    *,
    target_db_path: Path,
    target_upload_dir: Path,
) -> Dict[str, Any]:
    """Verify backup manifest and restore DB + uploads cleanly to an empty explicit destination.

    Refuses to overwrite an existing DB or non-empty upload directory so live services cannot be
    corrupted; operators must stop the service and clear/archive live files manually first.
    Promotes DB and uploads inside a rollback wrapper (removing newly moved uploads if DB
    promotion or post-promotion verification fails). Note: if target DB and target upload dir
    reside on different filesystems, directory/file moves are copy-based rather than single-syscall
    OS atomic renames, so rollback cleans up partial artifacts on error.
    """
    bdir = backup_dir.resolve()
    manifest_file = bdir / "manifest.json"
    if not manifest_file.exists() or manifest_file.is_symlink():
        raise BackupError(f"Backup manifest not found or unsafe: '{manifest_file}'")

    if target_db_path.is_symlink():
        raise BackupError(f"Target database path must not be a symlink: '{target_db_path}'")
    if target_upload_dir.exists() and target_upload_dir.is_symlink():
        raise BackupError(f"Target upload directory must not be a symlink: '{target_upload_dir}'")

    dest_db = target_db_path.resolve()
    dest_uploads = target_upload_dir.resolve()
    if dest_db.exists():
        raise BackupError(
            f"Refusing to overwrite existing target database '{dest_db}'. "
            "Stop the application service and archive/remove live DB + WAL/SHM files before restoring."
        )
    if dest_uploads.exists() and any(dest_uploads.iterdir()):
        raise BackupError(
            f"Refusing to overwrite non-empty target upload directory '{dest_uploads}'. "
            "Stop the application service and archive/empty the live directory before restoring."
        )

    backup_db = bdir / "database" / "subguard.db"
    backup_uploads = bdir / "uploads"
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest_files = manifest_data.get("files", {})

    manifest_check = verify_data_integrity(
        db_path=backup_db, upload_dir=backup_uploads, manifest_path=manifest_file
    )
    if not manifest_check["ok"]:
        raise BackupError(f"Backup verification failed before restore: {manifest_check}")

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    dest_uploads.parent.mkdir(parents=True, exist_ok=True)
    staging_root = dest_db.parent / f".restore_staging_{uuid4().hex[:8]}"
    staging_db = staging_root / "subguard.db"
    staging_uploads = staging_root / "uploads"

    dest_uploads_existed_empty = dest_uploads.exists()
    moved_uploads = False
    moved_db = False

    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        _copy_verified_backup_to_staging(
            backup_db=backup_db,
            backup_uploads=backup_uploads,
            staging_db=staging_db,
            staging_uploads=staging_uploads,
            manifest_files=manifest_files,
        )

        # Rewrite DB storage_path entries safely inside staging using validated components
        conn = sqlite3.connect(str(staging_db), timeout=30.0)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            with conn:
                rows = conn.execute("SELECT id, submission_id, storage_path FROM documents").fetchall()
                for doc_id, sub_id, sp in rows:
                    if sp:
                        safe_sub_id, safe_fname = _extract_safe_doc_rel_parts(sub_id, sp)
                        rewritten_target = (dest_uploads / safe_sub_id / safe_fname).resolve()
                        if not rewritten_target.is_relative_to(dest_uploads):
                            raise BackupError(
                                f"Rewritten storage_path escapes destination uploads: '{rewritten_target}'"
                            )
                        conn.execute(
                            "UPDATE documents SET storage_path = ? WHERE id = ?",
                            (str(rewritten_target), doc_id),
                        )
        finally:
            conn.close()

        # Re-run full SQLite integrity, foreign-key, and document reference checks after rewrite
        post_integrity, post_fk_violations = _sqlite_integrity_and_fk_check(staging_db)
        if post_integrity.lower() != "ok":
            raise BackupError(f"Post-rewrite DB integrity_check failed: {post_integrity}")
        if post_fk_violations:
            raise BackupError(f"Post-rewrite DB foreign_key_check failed: {post_fk_violations}")

        missing_post, _ = _verify_db_document_references(
            staging_db, staging_uploads, expected_storage_root=dest_uploads
        )
        if missing_post:
            raise BackupError(f"Post-rewrite document reference verification failed: {missing_post}")

        # Promote from staging into empty destination with rollback if DB move fails
        if dest_uploads_existed_empty:
            dest_uploads.rmdir()
        shutil.move(str(staging_uploads), str(dest_uploads))
        moved_uploads = True

        shutil.move(str(staging_db), str(dest_db))
        moved_db = True

        # Final post-promotion verification (upload SHA-256 hashes + DB integrity/FKs/refs)
        final_upload_mismatches = _verify_manifest_entries(
            manifest_files,
            target_db=dest_db,
            target_uploads=dest_uploads,
            verify_db_hash=False,
        )
        if final_upload_mismatches:
            raise BackupError(
                f"Post-restore upload hash verification failed: {final_upload_mismatches}"
            )
        final_check = verify_data_integrity(
            db_path=dest_db,
            upload_dir=dest_uploads,
            expected_storage_root=dest_uploads,
        )
        if not final_check["ok"]:
            raise BackupError(f"Post-restore target verification failed: {final_check}")

        shutil.rmtree(staging_root, ignore_errors=True)
        return {
            "status": "restored",
            "target_db_path": str(dest_db),
            "target_upload_dir": str(dest_uploads),
            "schema_version": final_check["schema_version"],
            "integrity_check": final_check["integrity_check"],
            "upload_hashes_verified": True,
            "next_step": "Start the application service and verify GET /health and sample document access.",
        }
    except Exception:
        if moved_db and dest_db.exists():
            try:
                dest_db.unlink()
            except OSError:
                pass
        if moved_uploads and dest_uploads.exists():
            shutil.rmtree(dest_uploads, ignore_errors=True)
        if dest_uploads_existed_empty and not dest_uploads.exists():
            try:
                dest_uploads.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SubGuard AI Backup, Verify, and Restore CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    b_cmd = sub.add_parser("backup", help="Create online SQLite + uploads snapshot backup")
    b_cmd.add_argument("--output-dir", required=True, help="Directory to store completed backup")
    b_cmd.add_argument("--db-path", default=None, help="Optional explicit SQLite DB path")
    b_cmd.add_argument("--upload-dir", default=None, help="Optional explicit upload dir path")
    b_cmd.add_argument("--name", default=None, help="Optional backup folder name")

    v_cmd = sub.add_parser("verify", help="Verify SQLite integrity, FKs, and upload file consistency")
    v_cmd.add_argument("--db-path", default=None, help="Optional explicit SQLite DB path")
    v_cmd.add_argument("--upload-dir", default=None, help="Optional explicit upload dir path")
    v_cmd.add_argument("--manifest", default=None, help="Optional backup manifest.json path")

    r_cmd = sub.add_parser("restore", help="Restore verified backup to an explicit empty target")
    r_cmd.add_argument("--backup-dir", required=True, help="Path to existing verified backup directory")
    r_cmd.add_argument("--target-db-path", required=True, help="Explicit empty destination DB path")
    r_cmd.add_argument("--target-upload-dir", required=True, help="Explicit empty destination uploads path")

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            res = backup_application_data(
                Path(args.output_dir),
                db_path=Path(args.db_path) if args.db_path else None,
                upload_dir=Path(args.upload_dir) if args.upload_dir else None,
                name=args.name,
            )
            print(json.dumps(res, indent=2))
            return 0
        if args.command == "verify":
            res = verify_data_integrity(
                db_path=Path(args.db_path) if args.db_path else None,
                upload_dir=Path(args.upload_dir) if args.upload_dir else None,
                manifest_path=Path(args.manifest) if args.manifest else None,
            )
            print(json.dumps(res, indent=2))
            return 0 if res["ok"] else 1
        if args.command == "restore":
            res = restore_application_data(
                Path(args.backup_dir),
                target_db_path=Path(args.target_db_path),
                target_upload_dir=Path(args.target_upload_dir),
            )
            print(json.dumps(res, indent=2))
            return 0
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
