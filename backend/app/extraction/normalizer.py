"""Deterministic normalization utilities for extracted insurance values.

Ensures:
- Currency strings ($1,000,000, 1M, $500K) convert to integer cents without floating-point errors
- Dates (01/01/2025, 2025-01-01, Jan 1, 2025) parse safely into datetime.date
- Coverage names and endorsement types normalize to validated Enums
- Ambiguous or unparseable limits/dates flag explicit uncertainty without guessing
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional, Tuple

from app.schemas.insurance import CoverageType, EndorsementType


def normalize_currency_to_cents(raw_text: Optional[str]) -> Tuple[Optional[int], bool, str]:
    """Parse a raw currency string into integer cents.
    
    Returns:
        (amount_cents, is_ambiguous, reason)
    """
    if not raw_text or not raw_text.strip():
        return None, False, "EMPTY_INPUT"

    cleaned = raw_text.strip().replace("$", "").replace(",", "").strip()

    # Handle standard abbreviations like '1M' or '500K'
    m_match = re.match(r"^([\d.]+)\s*[mM](?:illion)?$", cleaned)
    if m_match:
        try:
            val = float(m_match.group(1))
            return int(round(val * 1_000_000 * 100)), False, "PARSED_MILLIONS"
        except ValueError:
            return None, True, "AMBIGUOUS_MILLIONS_FORMAT"

    k_match = re.match(r"^([\d.]+)\s*[kK]$", cleaned)
    if k_match:
        try:
            val = float(k_match.group(1))
            return int(round(val * 1_000 * 100)), False, "PARSED_THOUSANDS"
        except ValueError:
            return None, True, "AMBIGUOUS_THOUSANDS_FORMAT"

    # Numeric with optional decimals
    num_match = re.match(r"^(\d+)(?:\.(\d{1,2}))?$", cleaned)
    if num_match:
        dollars = int(num_match.group(1))
        cents_part = num_match.group(2) or "00"
        if len(cents_part) == 1:
            cents_part += "0"
        cents = int(cents_part)
        return (dollars * 100) + cents, False, "PARSED_DECIMAL"

    # If format contains multiple distinct numbers (e.g. '$1,000,000 / $2,000,000')
    if "/" in cleaned:
        return None, True, "SPLIT_OR_MULTIPLE_LIMITS_PRESENT"

    return None, True, f"UNPARSEABLE_CURRENCY_FORMAT: {raw_text}"


def normalize_date(raw_text: Optional[str]) -> Tuple[Optional[date], bool, str]:
    """Parse a date string deterministically into date object.
    
    Supported formats:
    - MM/DD/YYYY, M/D/YYYY, MM-DD-YYYY
    - YYYY-MM-DD
    - Month DD, YYYY (e.g. 'January 15, 2025', 'Jan 15, 2025')
    """
    if not raw_text or not raw_text.strip():
        return None, False, "EMPTY_INPUT"

    cleaned = re.sub(r"\s+", " ", raw_text.strip())

    formats = [
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%b %d %Y",
        "%m/%d/%y",  # 2-digit year
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
            # Sanity check year bounds for insurance policies
            if parsed.year < 1990 or parsed.year > 2100:
                return None, True, f"YEAR_OUT_OF_BOUNDS: {parsed.year}"
            return parsed, False, "PARSED_DATE"
        except ValueError:
            continue

    return None, True, f"UNPARSEABLE_DATE_FORMAT: {raw_text}"


def normalize_coverage_type(raw_type: str) -> Optional[CoverageType]:
    """Normalize raw string to CoverageType enum."""
    if not raw_type:
        return None
    cleaned = re.sub(r"[\s\-_]+", "_", raw_type.strip().lower())

    mapping = {
        "general_liability": CoverageType.GENERAL_LIABILITY,
        "commercial_general_liability": CoverageType.GENERAL_LIABILITY,
        "cgl": CoverageType.GENERAL_LIABILITY,
        "gl": CoverageType.GENERAL_LIABILITY,
        "auto_liability": CoverageType.AUTO_LIABILITY,
        "automobile_liability": CoverageType.AUTO_LIABILITY,
        "commercial_auto": CoverageType.AUTO_LIABILITY,
        "auto": CoverageType.AUTO_LIABILITY,
        "workers_comp": CoverageType.WORKERS_COMP,
        "workers_compensation": CoverageType.WORKERS_COMP,
        "wc": CoverageType.WORKERS_COMP,
        "umbrella_excess": CoverageType.UMBRELLA_EXCESS,
        "umbrella": CoverageType.UMBRELLA_EXCESS,
        "excess_liability": CoverageType.UMBRELLA_EXCESS,
        "umbrella_liability": CoverageType.UMBRELLA_EXCESS,
        "professional_liability": CoverageType.PROFESSIONAL_LIABILITY,
        "errors_and_omissions": CoverageType.PROFESSIONAL_LIABILITY,
        "e_and_o": CoverageType.PROFESSIONAL_LIABILITY,
    }
    return mapping.get(cleaned, CoverageType.OTHER)


def normalize_endorsement_type(raw_type: str) -> Optional[EndorsementType]:
    """Normalize raw string to EndorsementType enum."""
    if not raw_type:
        return None
    cleaned = re.sub(r"[\s\-_]+", "_", raw_type.strip().lower())

    if "additional_insured" in cleaned or "addl_insr" in cleaned:
        return EndorsementType.ADDITIONAL_INSURED
    if "waiver_of_subrogation" in cleaned or "subr_wvd" in cleaned or "waiver" in cleaned:
        return EndorsementType.WAIVER_OF_SUBROGATION
    if "primary" in cleaned and ("non_contributory" in cleaned or "noncontributory" in cleaned):
        return EndorsementType.PRIMARY_NONCONTRIBUTORY

    return EndorsementType.OTHER
