"""Programmatic generator of synthetic PDF evaluation fixtures for SubGuard AI.

Creates 8 distinct test documents covering:
1. Standard clean ACORD 25 COI.
2. Multi-page document with COI (page 1) and CG 20 10 endorsement (page 2).
3. Document with missing named insured and missing Workers' Comp section.
4. Document with umbrella excess liability and high limits.
5. Document with expired GL dates and inverted Auto dates.
6. Scanned image-only PDF with zero text layer (triggers OCR requirement).
7. Document with corporate suffix variation ('Apex Electrical Inc').
8. Document with blanket additional insured wording.

All documents use synthetic, non-PII, fictitious data.
"""

from __future__ import annotations

import os
from pathlib import Path
import pymupdf


def get_fixtures_dir() -> Path:
    """Return the path to the fixtures/pdfs directory."""
    fixtures_dir = Path(__file__).resolve().parent / "pdfs"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    return fixtures_dir


def generate_all_synthetic_fixtures() -> dict[str, Path]:
    """Generate all synthetic PDF test files if they don't already exist."""
    fixtures_dir = get_fixtures_dir()
    created = {}

    # 1. Standard ACORD 25 COI
    f1 = fixtures_dir / "synthetic_standard_acord25.pdf"
    if not f1.exists():
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        p.insert_text((50, 40), "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE", fontsize=12)
        p.insert_text((50, 70), "PRODUCER: SafeHarbor Insurance Agency, 100 Main St, Austin, TX", fontsize=10)
        p.insert_text((50, 100), "INSURED: Apex Electrical Contractors LLC", fontsize=11)
        p.insert_text((50, 115), "123 Industrial Pkwy, Austin, TX 78701", fontsize=9)
        p.insert_text((350, 100), "INSURER A: Travelers Indemnity Co", fontsize=10)
        p.insert_text((350, 115), "INSURER B: Texas Mutual Insurance", fontsize=10)
        p.insert_text((50, 160), "COVERAGES - COMMERCIAL GENERAL LIABILITY", fontsize=11)
        p.insert_text((50, 180), "POLICY NUMBER: GL-98765432", fontsize=10)
        p.insert_text((50, 195), "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026", fontsize=10)
        p.insert_text((350, 180), "EACH OCCURRENCE: $1,000,000", fontsize=10)
        p.insert_text((350, 195), "GENERAL AGGREGATE: $2,000,000", fontsize=10)
        p.insert_text((350, 210), "PRODUCTS - COMP/OP AGG: $2,000,000", fontsize=10)
        p.insert_text((50, 250), "AUTOMOBILE LIABILITY - ANY AUTO", fontsize=11)
        p.insert_text((50, 270), "POLICY NUMBER: AL-12345678", fontsize=10)
        p.insert_text((50, 285), "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026", fontsize=10)
        p.insert_text((350, 270), "COMBINED SINGLE LIMIT: $1,000,000", fontsize=10)
        p.insert_text((50, 320), "WORKERS COMPENSATION AND EMPLOYERS LIABILITY", fontsize=11)
        p.insert_text((50, 340), "POLICY NUMBER: WC-55554444", fontsize=10)
        p.insert_text((50, 355), "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026", fontsize=10)
        p.insert_text((350, 340), "E.L. EACH ACCIDENT: $1,000,000", fontsize=10)
        p.insert_text((350, 355), "E.L. DISEASE - EA EMPLOYEE: $1,000,000", fontsize=10)
        p.insert_text((350, 370), "E.L. DISEASE - POLICY LIMIT: $1,000,000", fontsize=10)
        p.insert_text((50, 420), "DESCRIPTION OF OPERATIONS / REMARKS:", fontsize=10)
        p.insert_text((50, 440), "General Contractor Corp is included as Additional Insured on General Liability.", fontsize=9)
        p.insert_text((50, 520), "CERTIFICATE HOLDER:", fontsize=10)
        p.insert_text((50, 540), "General Contractor Corp", fontsize=10)
        p.insert_text((50, 555), "500 Builder Way, Dallas, TX 75201", fontsize=9)
        doc.save(str(f1))
        doc.close()
    created["standard"] = f1

    # 2. Multi-page COI + Endorsement (CG 20 10)
    f2 = fixtures_dir / "synthetic_multipage_coi_endorsement.pdf"
    if not f2.exists():
        doc = pymupdf.open()
        # Page 1: COI
        p1 = doc.new_page(width=612, height=792)
        p1.insert_text((50, 40), "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", fontsize=12)
        p1.insert_text((50, 80), "INSURED: Apex Electrical Contractors LLC", fontsize=10)
        p1.insert_text((50, 120), "COMMERCIAL GENERAL LIABILITY - POLICY NUMBER: GL-98765432", fontsize=10)
        p1.insert_text((50, 135), "EFF: 01/01/2025  EXP: 01/01/2026", fontsize=10)
        p1.insert_text((350, 120), "EACH OCCURRENCE: $1,000,000", fontsize=10)
        p1.insert_text((350, 135), "GENERAL AGGREGATE: $2,000,000", fontsize=10)
        p1.insert_text((50, 200), "REMARKS: See attached endorsement CG 20 10 on page 2.", fontsize=9)
        p1.insert_text((50, 300), "CERTIFICATE HOLDER: General Contractor Corp", fontsize=10)

        # Page 2: Endorsement CG 20 10
        p2 = doc.new_page(width=612, height=792)
        p2.insert_text((50, 50), "POLICY NUMBER: GL-98765432", fontsize=10)
        p2.insert_text((50, 80), "THIS ENDORSEMENT CHANGES THE POLICY. PLEASE READ IT CAREFULLY.", fontsize=11)
        p2.insert_text((50, 110), "COMMERCIAL GENERAL LIABILITY CG 20 10 04 13", fontsize=12)
        p2.insert_text((50, 130), "ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS - SCHEDULED PERSON", fontsize=11)
        p2.insert_text((50, 170), "SCHEDULE Name of Additional Insured Person(s) or Organization(s):", fontsize=10)
        p2.insert_text((50, 190), "General Contractor Corp", fontsize=11)
        p2.insert_text((50, 220), "Location: All Texas Job Sites. Ongoing operations.", fontsize=10)
        doc.save(str(f2))
        doc.close()
    created["multipage"] = f2

    # 3. Missing Named Insured and Missing Workers' Comp Section
    f3 = fixtures_dir / "synthetic_missing_sections.pdf"
    if not f3.exists():
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        p.insert_text((50, 40), "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", fontsize=12)
        p.insert_text((50, 80), "PRODUCER: Lone Star Brokerage, Austin, TX", fontsize=10)
        # INSURED is intentionally omitted / left empty
        p.insert_text((50, 120), "INSURED: ", fontsize=10)
        p.insert_text((50, 160), "COMMERCIAL GENERAL LIABILITY", fontsize=11)
        p.insert_text((50, 180), "POLICY NUMBER: GL-11223344", fontsize=10)
        p.insert_text((50, 195), "EFF: 01/01/2025  EXP: 01/01/2026", fontsize=10)
        p.insert_text((350, 180), "EACH OCCURRENCE: $1,000,000", fontsize=10)
        p.insert_text((350, 195), "GENERAL AGGREGATE: $2,000,000", fontsize=10)
        # Workers' Comp section omitted entirely
        p.insert_text((50, 300), "CERTIFICATE HOLDER: General Contractor Corp", fontsize=10)
        doc.save(str(f3))
        doc.close()
    created["missing_sections"] = f3

    # 4. Umbrella Excess Liability and High Limits
    f4 = fixtures_dir / "synthetic_split_limits_and_umbrella.pdf"
    if not f4.exists():
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        p.insert_text((50, 40), "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", fontsize=12)
        p.insert_text((50, 80), "INSURED: Apex Electrical Contractors LLC", fontsize=10)
        p.insert_text((50, 130), "COMMERCIAL GENERAL LIABILITY - POLICY NUMBER: GL-98765432", fontsize=10)
        p.insert_text((50, 145), "EFF: 01/01/2025  EXP: 01/01/2026", fontsize=10)
        p.insert_text((350, 130), "EACH OCCURRENCE: $1,000,000", fontsize=10)
        p.insert_text((350, 145), "GENERAL AGGREGATE: $2,000,000", fontsize=10)
        p.insert_text((50, 190), "UMBRELLA LIAB / EXCESS LIAB - POLICY NUMBER: UMB-55667788", fontsize=10)
        p.insert_text((50, 205), "EFF: 01/01/2025  EXP: 01/01/2026", fontsize=10)
        p.insert_text((350, 190), "EACH OCCURRENCE: $5,000,000", fontsize=10)
        p.insert_text((350, 205), "AGGREGATE: $5,000,000", fontsize=10)
        p.insert_text((50, 300), "CERTIFICATE HOLDER: General Contractor Corp", fontsize=10)
        doc.save(str(f4))
        doc.close()
    created["umbrella"] = f4

    # 5. Expired GL and Inverted Auto Dates
    f5 = fixtures_dir / "synthetic_expired_and_inverted_dates.pdf"
    if not f5.exists():
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        p.insert_text((50, 40), "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", fontsize=12)
        p.insert_text((50, 80), "INSURED: Apex Electrical Contractors LLC", fontsize=10)
        p.insert_text((50, 130), "COMMERCIAL GENERAL LIABILITY - POLICY NUMBER: GL-98765432", fontsize=10)
        p.insert_text((50, 145), "POLICY EFF: 01/01/2023  EXP: 01/01/2024", fontsize=10)  # Expired
        p.insert_text((350, 130), "EACH OCCURRENCE: $1,000,000", fontsize=10)
        p.insert_text((50, 190), "AUTOMOBILE LIABILITY - POLICY NUMBER: AL-99887766", fontsize=10)
        p.insert_text((50, 205), "POLICY EFF: 01/01/2026  EXP: 01/01/2025", fontsize=10)  # Inverted!
        p.insert_text((350, 190), "COMBINED SINGLE LIMIT: $1,000,000", fontsize=10)
        p.insert_text((50, 300), "CERTIFICATE HOLDER: General Contractor Corp", fontsize=10)
        doc.save(str(f5))
        doc.close()
    created["expired_inverted"] = f5

    # 6. Scanned image-only PDF (no text layer)
    f6 = fixtures_dir / "synthetic_scanned_ocr.pdf"
    if not f6.exists():
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        # Create a small pixmap and insert as image so there is no text layer
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 100, 100), 1)
        pix.clear_with(240)
        p.insert_image(pymupdf.Rect(50, 50, 300, 300), pixmap=pix)
        doc.save(str(f6))
        doc.close()
    created["scanned"] = f6

    # 7. Corporate Suffix Variation ('Apex Electrical Inc')
    f7 = fixtures_dir / "synthetic_corporate_suffix_variation.pdf"
    if not f7.exists():
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        p.insert_text((50, 40), "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", fontsize=12)
        p.insert_text((50, 80), "INSURED: Apex Electrical Inc", fontsize=10)  # Inc instead of LLC
        p.insert_text((50, 130), "COMMERCIAL GENERAL LIABILITY - POLICY NUMBER: GL-98765432", fontsize=10)
        p.insert_text((50, 145), "POLICY EFF: 01/01/2025  EXP: 01/01/2026", fontsize=10)
        p.insert_text((350, 130), "EACH OCCURRENCE: $1,000,000", fontsize=10)
        p.insert_text((350, 145), "GENERAL AGGREGATE: $2,000,000", fontsize=10)
        p.insert_text((50, 300), "CERTIFICATE HOLDER: General Contractor Corp", fontsize=10)
        doc.save(str(f7))
        doc.close()
    created["suffix_variation"] = f7

    # 8. Blanket Endorsement Wording
    f8 = fixtures_dir / "synthetic_blanket_endorsement.pdf"
    if not f8.exists():
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        p.insert_text((50, 40), "COMMERCIAL GENERAL LIABILITY POLICY ENDORSEMENT", fontsize=12)
        p.insert_text((50, 70), "POLICY NUMBER: GL-98765432", fontsize=10)
        p.insert_text((50, 100), "ADDITIONAL INSURED - AUTOMATIC STATUS WHEN REQUIRED IN CONSTRUCTION AGREEMENT", fontsize=11)
        p.insert_text((50, 140), "Who Is An Insured is amended to include as an additional insured any person or", fontsize=10)
        p.insert_text((50, 155), "organization whom you have agreed by written contract to add as additional insured.", fontsize=10)
        doc.save(str(f8))
        doc.close()
    created["blanket"] = f8

    return created


if __name__ == "__main__":
    generated = generate_all_synthetic_fixtures()
    print(f"Generated {len(generated)} synthetic fixtures:")
    for key, path in generated.items():
        print(f"  {key}: {path}")
