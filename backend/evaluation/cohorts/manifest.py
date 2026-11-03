"""Manifest verification and environmental metadata capture for SubGuard AI benchmarks.

Ensures reproducibility:
1. Validates that every PDF file listed in the manifest exists.
2. Validates that the SHA-256 hash of each PDF matches the manifest.
3. Validates that the annotations file exists and matches its recorded hash.
4. Gathers sanitized system and runtime metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def compute_file_sha256(filepath: Path) -> str:
    """Compute hex SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_manifest(manifest_path: Path) -> Dict[str, Any]:
    """Load JSON manifest from disk."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found at {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_manifest(
    manifest_path: Path,
    base_dir: Optional[Path] = None,
) -> Tuple[bool, List[str]]:
    """Verify all files and SHA-256 hashes against manifest.
    
    Returns:
        (is_valid, list_of_error_messages)
    """
    if base_dir is None:
        base_dir = manifest_path.parent.parent

    errors: List[str] = []
    try:
        manifest = load_manifest(manifest_path)
    except Exception as e:
        return False, [f"Failed to load manifest: {str(e)}"]

    # Verify annotations file
    ann_rel = manifest.get("annotations_file")
    ann_hash = manifest.get("annotations_sha256")
    if ann_rel and ann_hash:
        ann_path = base_dir.parent / ann_rel
        if not ann_path.exists():
            errors.append(f"Annotations file missing: {ann_path}")
        else:
            actual_ann_hash = compute_file_sha256(ann_path)
            if actual_ann_hash != ann_hash:
                errors.append(
                    f"Annotations hash mismatch: expected {ann_hash[:12]}..., got {actual_ann_hash[:12]}..."
                )

    # Verify each document
    documents = manifest.get("documents", [])
    if len(documents) == 0:
        errors.append("Manifest documents list is empty")

    for doc in documents:
        rel_path = doc.get("relative_path")
        expected_hash = doc.get("sha256")
        doc_id = doc.get("document_id")

        if not rel_path or not expected_hash:
            errors.append(f"Invalid manifest document entry: {doc}")
            continue

        file_path = base_dir / rel_path
        if not file_path.exists():
            errors.append(f"Document file missing for {doc_id}: {file_path}")
            continue

        actual_hash = compute_file_sha256(file_path)
        if actual_hash != expected_hash:
            errors.append(
                f"SHA-256 hash mismatch for {doc_id} ({rel_path}): expected {expected_hash[:12]}..., got {actual_hash[:12]}..."
            )

    is_valid = len(errors) == 0
    return is_valid, errors


def get_environment_metadata() -> Dict[str, str]:
    """Capture sanitized environment metadata with zero credentials or secrets."""
    git_hash = "unknown"
    git_branch = "unknown"
    try:
        git_hash = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=5
        ).strip()
        git_branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True, timeout=5
        ).strip()
    except Exception:
        pass

    import pydantic
    import pymupdf

    return {
        "os": f"{platform.system()} {platform.release()}",
        "python_version": sys.version.split()[0],
        "git_commit": git_hash,
        "git_branch": git_branch,
        "pydantic_version": getattr(pydantic, "__version__", "unknown"),
        "pymupdf_version": getattr(pymupdf, "__version__", "unknown"),
    }
