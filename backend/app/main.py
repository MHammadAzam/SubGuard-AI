"""Minimal FastAPI application boundary for SubGuard AI compliance evaluator.

Exposes a stateless evaluation endpoint. No databases, external APIs, or background
workers are required to evaluate compliance.
"""

from datetime import date
from typing import List

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from app.compliance.evaluator import ComplianceEvaluator
from app.schemas.insurance import (
    ComplianceReviewResult,
    ExtractedDocumentSet,
    InsuranceRequirement,
)

app = FastAPI(
    title="SubGuard AI - Compliance Engine API",
    version="0.1.0",
    description="Deterministic evaluation service for insurance compliance documents.",
)

evaluator = ComplianceEvaluator()


class EvaluationRequest(BaseModel):
    """Payload for on-demand deterministic compliance evaluation."""
    document_set: ExtractedDocumentSet
    requirements: List[InsuranceRequirement] = Field(min_length=1)
    evaluation_date: date = Field(description="Explicit evaluation date for policy expiration checks")


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "compliance-evaluator"}


@app.post(
    "/api/v1/compliance/evaluate",
    response_model=ComplianceReviewResult,
    status_code=status.HTTP_200_OK,
    summary="Evaluate an extracted document set against insurance requirements deterministically",
)
def evaluate_compliance(request: EvaluationRequest) -> ComplianceReviewResult:
    """Evaluate insurance documents against requirements.
    
    This endpoint is stateless and pure:
    - AI extraction does not decide compliance.
    - All requirements are evaluated deterministically using Python business logic.
    - Uncertainty, missing fields, and ambiguities are surfaced explicitly.
    """
    try:
        return evaluator.evaluate(
            document_set=request.document_set,
            requirements=request.requirements,
            evaluation_date=request.evaluation_date,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Evaluation failed: {str(exc)}",
        ) from exc
