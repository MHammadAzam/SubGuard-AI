"""Repository layer for SubGuard AI persistence.

Encapsulates all SQL interactions for submissions, documents, and human reviews.
Uses parameterized queries to prevent SQL injection.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from app.persistence.database import get_connection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubmissionRepository:
    """Data access repository for submissions, documents, and reviews."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path

    def create_submission(
        self,
        submission_id: str,
        name: str,
        overall_status: str,
        processing_status: str,
        provider: str,
        evaluation_date: str,
        result_data: Dict[str, Any],
        subcontractor_name: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a new submission record."""
        ts = created_at or _now_iso()
        serialized = json.dumps(result_data)
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO submissions (
                        id, name, created_at, overall_status, processing_status,
                        provider, evaluation_date, subcontractor_name, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        submission_id,
                        name,
                        ts,
                        overall_status,
                        processing_status,
                        provider,
                        evaluation_date,
                        subcontractor_name,
                        serialized,
                    ),
                )
        finally:
            conn.close()

        return self.get_submission_by_id(submission_id)  # type: ignore

    def add_document(
        self,
        document_id: str,
        submission_id: str,
        original_filename: str,
        document_type: str,
        storage_path: str,
        file_size_bytes: int,
        page_count: int = 0,
        processing_status: str = "processed",
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an uploaded document associated with a submission."""
        ts = _now_iso()
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO documents (
                        id, submission_id, original_filename, document_type,
                        storage_path, file_size_bytes, page_count,
                        upload_timestamp, processing_status, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        submission_id,
                        original_filename,
                        document_type,
                        storage_path,
                        file_size_bytes,
                        page_count,
                        ts,
                        processing_status,
                        error_message,
                    ),
                )
        finally:
            conn.close()

        return {
            "id": document_id,
            "submission_id": submission_id,
            "original_filename": original_filename,
            "document_type": document_type,
            "storage_path": storage_path,
            "file_size_bytes": file_size_bytes,
            "page_count": page_count,
            "upload_timestamp": ts,
            "processing_status": processing_status,
            "error_message": error_message,
        }

    def get_submission_by_id(self, submission_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a submission by its unique identifier, including its documents and reviews."""
        conn = get_connection(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, created_at, overall_status, processing_status,
                       provider, evaluation_date, subcontractor_name, result_json
                FROM submissions
                WHERE id = ?
                """,
                (submission_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            sub_dict = dict(row)
            try:
                sub_dict["result"] = json.loads(sub_dict["result_json"])
            except Exception:
                sub_dict["result"] = {}

            cursor.execute(
                """
                SELECT id, original_filename, document_type, storage_path,
                       file_size_bytes, page_count, upload_timestamp,
                       processing_status, error_message
                FROM documents
                WHERE submission_id = ?
                ORDER BY upload_timestamp ASC
                """,
                (submission_id,),
            )
            sub_dict["documents"] = [dict(d) for d in cursor.fetchall()]

            cursor.execute(
                """
                SELECT id, finding_id, reason_code, review_status,
                       reviewer_notes, reviewer_name, reviewer_user_id, created_at, resolved_at
                FROM review_records
                WHERE submission_id = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (submission_id,),
            )
            sub_dict["reviews"] = [dict(r) for r in cursor.fetchall()]

            return sub_dict
        finally:
            conn.close()

    def list_submissions(
        self,
        limit: int = 50,
        offset: int = 0,
        status_filter: Optional[str] = None,
        search_query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List past submissions with optional status filter and search query."""
        conn = get_connection(self.db_path)
        try:
            cursor = conn.cursor()
            query = """
                SELECT s.id, s.name, s.created_at, s.overall_status, s.processing_status,
                       s.provider, s.evaluation_date, s.subcontractor_name,
                       COUNT(d.id) AS document_count
                FROM submissions s
                LEFT JOIN documents d ON s.id = d.submission_id
            """
            params: List[Any] = []
            conditions: List[str] = []

            if status_filter:
                conditions.append("s.overall_status = ?")
                params.append(status_filter.lower())

            if search_query:
                conditions.append("(s.name LIKE ? OR s.subcontractor_name LIKE ?)")
                like_term = f"%{search_query.strip()}%"
                params.extend([like_term, like_term])

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " GROUP BY s.id ORDER BY s.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    def get_dashboard_stats(self) -> Dict[str, Any]:
        """Compute real aggregate statistics for the dashboard."""
        conn = get_connection(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_submissions,
                    SUM(CASE WHEN overall_status = 'compliant' THEN 1 ELSE 0 END) AS compliant,
                    SUM(CASE WHEN overall_status = 'non_compliant' THEN 1 ELSE 0 END) AS non_compliant,
                    SUM(CASE WHEN overall_status = 'requires_human_review' THEN 1 ELSE 0 END) AS requires_human_review,
                    SUM(CASE WHEN overall_status = 'missing_evidence' THEN 1 ELSE 0 END) AS missing_evidence,
                    SUM(CASE WHEN overall_status = 'unable_to_determine' THEN 1 ELSE 0 END) AS unable_to_determine
                FROM submissions
                """
            )
            row = cursor.fetchone()
            if not row:
                return {
                    "total_submissions": 0,
                    "compliant": 0,
                    "non_compliant": 0,
                    "requires_human_review": 0,
                    "missing_evidence": 0,
                    "unable_to_determine": 0,
                }
            return {
                "total_submissions": row["total_submissions"] or 0,
                "compliant": row["compliant"] or 0,
                "non_compliant": row["non_compliant"] or 0,
                "requires_human_review": row["requires_human_review"] or 0,
                "missing_evidence": row["missing_evidence"] or 0,
                "unable_to_determine": row["unable_to_determine"] or 0,
            }
        finally:
            conn.close()

    def add_review_record(
        self,
        submission_id: str,
        review_status: str,
        reviewer_notes: str,
        finding_id: Optional[str] = None,
        reason_code: Optional[str] = None,
        reviewer_name: str = "Compliance Reviewer",
        update_submission_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a human review action and optionally update submission overall status."""
        review_id = f"rev_{uuid4().hex[:12]}"
        ts = _now_iso()
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO review_records (
                        id, submission_id, finding_id, reason_code, review_status,
                        reviewer_notes, reviewer_name, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        submission_id,
                        finding_id,
                        reason_code,
                        review_status,
                        reviewer_notes,
                        reviewer_name,
                        ts,
                        ts,
                    ),
                )
                if update_submission_status:
                    conn.execute(
                        "UPDATE submissions SET overall_status = ? WHERE id = ?",
                        (update_submission_status, submission_id),
                    )
        finally:
            conn.close()

        return {
            "id": review_id,
            "submission_id": submission_id,
            "finding_id": finding_id,
            "reason_code": reason_code,
            "review_status": review_status,
            "reviewer_notes": reviewer_notes,
            "reviewer_name": reviewer_name,
            "created_at": ts,
            "resolved_at": ts,
        }

    def record_human_review_atomic(
        self,
        submission_id: str,
        evaluate_decision: Callable[[Optional[Dict[str, Any]], List[Dict[str, Any]]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Atomically evaluate and persist a human-review decision and resulting status.

        Acquires an immediate SQLite write lock before reading the submission and existing
        append-only review history so concurrent review requests cannot race past
        per-finding invariants. Rolls back completely if any validation or DB step fails.
        """
        review_id = f"rev_{uuid4().hex[:12]}"
        ts = _now_iso()
        conn = get_connection(self.db_path)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, created_at, overall_status, processing_status,
                       provider, evaluation_date, subcontractor_name, result_json
                FROM submissions
                WHERE id = ?
                """,
                (submission_id,),
            )
            row = cursor.fetchone()
            sub_dict: Optional[Dict[str, Any]] = None
            existing_reviews_asc: List[Dict[str, Any]] = []
            if row:
                sub_dict = dict(row)
                try:
                    sub_dict["result"] = json.loads(sub_dict["result_json"])
                except Exception:
                    sub_dict["result"] = {}

                cursor.execute(
                    """
                    SELECT id, finding_id, reason_code, review_status,
                           reviewer_notes, reviewer_name, reviewer_user_id, created_at, resolved_at
                    FROM review_records
                    WHERE submission_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (submission_id,),
                )
                existing_reviews_asc = [dict(r) for r in cursor.fetchall()]

            decision = evaluate_decision(sub_dict, existing_reviews_asc)

            cursor.execute(
                """
                INSERT INTO review_records (
                    id, submission_id, finding_id, reason_code, review_status,
                    reviewer_notes, reviewer_name, reviewer_user_id, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    submission_id,
                    decision.get("finding_id"),
                    decision.get("reason_code"),
                    decision["review_status"],
                    decision["reviewer_notes"],
                    decision["reviewer_name"],
                    decision.get("reviewer_user_id"),
                    ts,
                    ts,
                ),
            )
            cursor.execute(
                "UPDATE submissions SET overall_status = ? WHERE id = ?",
                (decision["overall_status"], submission_id),
            )
            conn.execute("COMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

        return {
            "overall_status": decision["overall_status"],
            "review": {
                "id": review_id,
                "submission_id": submission_id,
                "finding_id": decision.get("finding_id"),
                "reason_code": decision.get("reason_code"),
                "review_status": decision["review_status"],
                "reviewer_notes": decision["reviewer_notes"],
                "reviewer_name": decision["reviewer_name"],
                "reviewer_user_id": decision.get("reviewer_user_id"),
                "created_at": ts,
                "resolved_at": ts,
            },
        }
