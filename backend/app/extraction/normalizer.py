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
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from app.schemas.insurance import CoverageType, EndorsementType

_US_GROUPED_INT_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_PLAIN_INT_RE = re.compile(r"^\d+$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{2,4})$")


def _parse_currency_body(body: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate US currency numeric grouping and decimal structure before Decimal parsing."""
    if not body or body.count(".") > 1:
        return None, "AMBIGUOUS_NUMBER_SEPARATORS"

    if "." in body:
        int_part, frac_part = body.split(".", 1)
        if not int_part or not frac_part or not frac_part.isdigit():
            return None, "AMBIGUOUS_NUMBER_SEPARATORS"
    else:
        int_part, frac_part = body, ""

    if "," in int_part:
        if not _US_GROUPED_INT_RE.match(int_part):
            return None, "AMBIGUOUS_NUMBER_SEPARATORS"
        clean_int = int_part.replace(",", "")
    else:
        if not _PLAIN_INT_RE.match(int_part):
            return None, "AMBIGUOUS_NUMBER_SEPARATORS"
        clean_int = int_part

    normalized = f"{clean_int}.{frac_part}" if frac_part else clean_int
    return normalized, None


def normalize_currency_to_cents(raw_text: Optional[str]) -> Tuple[Optional[int], bool, str]:
    """Parse a raw currency string into exact integer cents using Decimal arithmetic.

    Rejects / flags ambiguous, negative, split, separator-ambiguous, or sub-cent inputs
    instead of guessing or applying binary floating-point rounding.

    Returns:
        (amount_cents, is_ambiguous, reason)
    """
    if not raw_text or not raw_text.strip():
        return None, False, "EMPTY_INPUT"

    raw_stripped = raw_text.strip()

    # Split / dual limits such as "$1,000,000 / $2,000,000"
    if "/" in raw_stripped:
        return None, True, "SPLIT_OR_MULTIPLE_LIMITS_PRESENT"

    # Negative limits (explicit '-' or accounting parentheses '($1,000)') are invalid
    if raw_stripped.startswith("-") or (raw_stripped.startswith("(") and raw_stripped.endswith(")")):
        return None, True, "NEGATIVE_AMOUNT_UNSUPPORTED"

    # Optional leading '$'
    cleaned = raw_stripped
    if cleaned.startswith("$"):
        cleaned = cleaned[1:].strip()
    if "$" in cleaned or "-" in cleaned:
        return None, True, f"UNPARSEABLE_CURRENCY_FORMAT: {raw_text}"

    # Extract optional K / M / Million suffix
    match = re.match(r"^([0-9][0-9,\.]*)\s*(?:([mM])(?:illion)?|([kK]))?$", cleaned)
    if not match:
        return None, True, f"UNPARSEABLE_CURRENCY_FORMAT: {raw_text}"

    body = match.group(1)
    is_millions = match.group(2) is not None
    is_thousands = match.group(3) is not None

    clean_num, sep_err = _parse_currency_body(body)
    if sep_err or clean_num is None:
        return None, True, sep_err or "AMBIGUOUS_NUMBER_SEPARATORS"

    try:
        dec_val = Decimal(clean_num)
    except InvalidOperation:
        return None, True, f"UNPARSEABLE_CURRENCY_FORMAT: {raw_text}"

    if dec_val < 0:
        return None, True, "NEGATIVE_AMOUNT_UNSUPPORTED"

    if is_millions:
        multiplier = Decimal(100_000_000)
        reason = "PARSED_MILLIONS"
    elif is_thousands:
        multiplier = Decimal(100_000)
        reason = "PARSED_THOUSANDS"
    else:
        multiplier = Decimal(100)
        reason = "PARSED_DECIMAL"

    cents_dec = dec_val * multiplier
    if cents_dec != cents_dec.to_integral_value():
        return None, True, "EXCESSIVE_DECIMAL_PRECISION"

    return int(cents_dec), False, reason


def is_ambiguous_numeric_date(raw_text: Optional[str]) -> bool:
    """Return True if raw_text is a slash/dash numeric date whose month/day order is inherently ambiguous."""
    if not raw_text or not raw_text.strip():
        return False
    cleaned = re.sub(r"\s+", " ", raw_text.strip())
    m = _NUMERIC_DATE_RE.match(cleaned)
    if not m:
        return False
    p1, p2, y_str = int(m.group(1)), int(m.group(3)), m.group(4)
    if len(y_str) == 2:
        return True
    return 1 <= p1 <= 12 and 1 <= p2 <= 12 and p1 != p2


def normalize_date(
    raw_text: Optional[str],
    date_format_policy: Optional[str] = "US_MDY",
) -> Tuple[Optional[date], bool, str]:
    """Parse a date string deterministically into a datetime.date object.

    Supported unambiguous formats:
    - ISO YYYY-MM-DD
    - Textual months (e.g. 'January 15, 2025', 'Oct 31, 2026', '15 Jan 2025')

    Numeric slash/dash formats (e.g. '01/02/2025', '01-02-2025'):
    - When month and day are both <= 12 and distinct, interpretation requires an explicit
      date_format_policy ('US_MDY' or 'EU_DMY'). When date_format_policy is None or 'STRICT',
      ambiguous numeric dates fail closed as ambiguous instead of guessing.
    """
    if not raw_text or not raw_text.strip():
        return None, False, "EMPTY_INPUT"

    cleaned = re.sub(r"\s+", " ", raw_text.strip())
    policy = date_format_policy.upper() if date_format_policy else None

    def _validate_bounds(parsed_d: date, reason: str) -> Tuple[Optional[date], bool, str]:
        if parsed_d.year < 1990 or parsed_d.year > 2100:
            return None, True, f"YEAR_OUT_OF_BOUNDS: {parsed_d.year}"
        return parsed_d, False, reason

    # 1. ISO YYYY-MM-DD
    iso_m = _ISO_DATE_RE.match(cleaned)
    if iso_m:
        try:
            parsed = date(int(iso_m.group(1)), int(iso_m.group(2)), int(iso_m.group(3)))
            return _validate_bounds(parsed, "PARSED_ISO_DATE")
        except ValueError:
            return None, True, f"IMPOSSIBLE_DATE: {raw_text}"

    # 2. Textual month formats (unambiguous)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
            return _validate_bounds(parsed, "PARSED_TEXTUAL_DATE")
        except ValueError:
            continue

    # 3. Numeric slash/dash dates
    num_m = _NUMERIC_DATE_RE.match(cleaned)
    if num_m:
        p1 = int(num_m.group(1))
        p2 = int(num_m.group(3))
        y_str = num_m.group(4)

        if len(y_str) == 2:
            if policy in (None, "STRICT"):
                return None, True, f"AMBIGUOUS_TWO_DIGIT_YEAR: {raw_text}"
            year = 2000 + int(y_str)
        else:
            year = int(y_str)

        both_valid_md_dm = (1 <= p1 <= 12 and 1 <= p2 <= 12 and p1 != p2)
        if both_valid_md_dm and policy in (None, "STRICT"):
            return None, True, f"AMBIGUOUS_DATE_FORMAT: {raw_text}"

        try:
            if policy == "EU_DMY":
                parsed = date(year, p2, p1)
            else:
                if policy not in ("US_MDY", None, "STRICT"):
                    return None, True, f"UNSUPPORTED_DATE_POLICY: {date_format_policy}"
                parsed = date(year, p1, p2)
            return _validate_bounds(parsed, "PARSED_DATE")
        except ValueError:
            return None, True, f"IMPOSSIBLE_DATE: {raw_text}"

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
