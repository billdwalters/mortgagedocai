"""
MortgageDocAI — Form Fill Registry and Filler

Loads pipeline output JSONs, maps extracted values to Excel template cells,
and produces pre-filled .xlsx files. Cells the pipeline can't fill are left
empty for manual entry. Formulas in templates are preserved.

Usage:
    from formfill import fill_form, FORM_TEMPLATES
    audit = fill_form("income_calc_w2", profiles_dir, output_path,
                      loan_id="12345", run_id="2026-01-01T120000Z")
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import openpyxl

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class CellType(Enum):
    NUMBER = "number"
    TEXT = "text"
    CURRENCY = "currency"
    PERCENTAGE = "percentage"


@dataclass(frozen=True)
class FieldMapping:
    cell: str           # e.g. "C4"
    source: str         # "income_analysis", "dti", "decision", "conditions"
    json_path: str      # "monthly_income_total.value"
    cell_type: CellType
    sheet: str = ""     # "" = first sheet; named sheet otherwise
    label: str = ""     # human-readable for audit


@dataclass(frozen=True)
class FormTemplate:
    template_id: str
    display_name: str
    category: str       # "Income", "FHA", "VA"
    filename: str       # e.g. "income_calc_w2.xlsx"
    description: str
    mappings: tuple[FieldMapping, ...]


# ---------------------------------------------------------------------------
# Source file mapping
# ---------------------------------------------------------------------------
_SOURCE_FILE_MAP = {
    "income_analysis": ("income_analysis", "income_analysis.json"),
    "dti":             ("income_analysis", "dti.json"),
    "decision":        ("uw_decision",     "decision.json"),
    "conditions":      ("uw_conditions",   "conditions.json"),
}


# ---------------------------------------------------------------------------
# Template directory
# ---------------------------------------------------------------------------
_FORMS_DIR = Path(__file__).resolve().parent.parent / "webui" / "forms"


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

# ---- Income Calc (W2) ----
# Template layout (data-entry cells only, no formulas):
#   Row 4:  C4=Borrower Name
#   Row 11: C11=YTD Earnings, G11=# months  → J11=C11/G11 (formula)
#   Row 12: C12=W2 Year 1,    G12=# months  → J12=C12/G12 (formula)
#   Row 13: C13=W2 Year 2,    G13=# months  → J13=C13/G13 (formula)
#   Row 25: C25=Monthly salary               → J25=C25*1 (formula)
#   Row 39: C39=YTD OT/Bonus, G39=# months  → J39=C39/G39 (formula)
#   Row 40: C40=Past year OT,  G40=# months → J40=C40/G40 (formula)
# NOTE: C15-C20, C34 are formulas — do NOT overwrite.
_INCOME_CALC_W2_MAPPINGS = (
    # Primary income component → YTD earnings row
    FieldMapping("C11", "income_analysis", "monthly_income_total.components.0.period_total", CellType.CURRENCY,
                 label="YTD earnings (1st income component period total)"),
    FieldMapping("G11", "income_analysis", "monthly_income_total.components.0.period_months", CellType.NUMBER,
                 label="YTD months (1st income component)"),
    # Second income component → W2 Year 1 row (if co-borrower / 2nd P&L)
    FieldMapping("C12", "income_analysis", "monthly_income_total.components.1.period_total", CellType.CURRENCY,
                 label="W2 Year 1 (2nd income component period total)"),
    FieldMapping("G12", "income_analysis", "monthly_income_total.components.1.period_months", CellType.NUMBER,
                 label="W2 Year 1 months (2nd income component)"),
    # Monthly salary field (monthly income total for quick reference)
    FieldMapping("C25", "dti", "monthly_income_total", CellType.CURRENCY,
                 label="Monthly salary (pipeline monthly income total)"),
    # PITIA / housing payment used
    FieldMapping("C30", "dti", "housing_payment_used", CellType.CURRENCY,
                 label="Housing payment (PITIA)"),
    # Monthly liabilities
    FieldMapping("C39", "dti", "monthly_debt_total", CellType.CURRENCY,
                 label="Monthly debt total (as OT/Bonus placeholder)"),
)

# ---- FHA Max Mortgage Calc ----
# Simple sheet: I10=UPB, I11=interest due, I5=county limit, I7=adjusted value
# Streamline sheet: I6=outstanding balance, I12=original FHA amount
# Pipeline extracts monthly liabilities and PITIA — map what we can.
_FHA_MAX_MORTGAGE_MAPPINGS = (
    # Simple sheet — existing mortgage balance approximation
    FieldMapping("I10", "dti", "housing_payment_used", CellType.CURRENCY,
                 sheet="Simple", label="Monthly PITIA (as reference for existing mortgage)"),
    # Streamline sheet — PITIA
    FieldMapping("I6", "dti", "housing_payment_used", CellType.CURRENCY,
                 sheet=" Streamline (Owner-Occupied)", label="Monthly PITIA"),
)

# ---- VA IRRRL Recoupment Calc ----
# Sheet1: B11=existing loan amt, C11=proposed loan amt, B14=existing rate,
#   C14=proposed rate, B15=existing P&I, C15=proposed P&I (formula =C15)
#   C16=VA funding fee, C17=closing costs, B12/C12=loan term
_VA_IRRRL_MAPPINGS = (
    # Existing monthly P&I from PITIA extraction
    FieldMapping("B15", "income_analysis", "proposed_pitia.value", CellType.CURRENCY,
                 label="Existing monthly P&I (from PITIA)"),
    # Monthly income (context)
    FieldMapping("B11", "dti", "monthly_income_total", CellType.CURRENCY,
                 label="Existing loan amount (placeholder: monthly income)"),
    # Monthly debt total
    FieldMapping("C18", "dti", "monthly_debt_total", CellType.CURRENCY,
                 label="Monthly debt total (closing costs placeholder)"),
    # Front-end DTI
    FieldMapping("B14", "dti", "front_end_dti", CellType.PERCENTAGE,
                 label="Front-end DTI (existing rate placeholder)"),
    # Back-end DTI
    FieldMapping("C14", "dti", "back_end_dti", CellType.PERCENTAGE,
                 label="Back-end DTI (proposed rate placeholder)"),
)

FORM_TEMPLATES: dict[str, FormTemplate] = {}


def _register(t: FormTemplate) -> None:
    FORM_TEMPLATES[t.template_id] = t


_register(FormTemplate(
    template_id="income_calc_w2",
    display_name="Income Calc (W2)",
    category="Income",
    filename="income_calc_w2.xlsx",
    description="W-2 income calculation worksheet. Pre-fills income totals and DTI from pipeline extraction.",
    mappings=_INCOME_CALC_W2_MAPPINGS,
))

_register(FormTemplate(
    template_id="fha_max_mortgage_calc",
    display_name="FHA Max Mortgage Calc",
    category="FHA",
    filename="fha_max_mortgage_calc.xlsx",
    description="FHA maximum mortgage amount calculator. Pre-fills existing mortgage balances from pipeline data.",
    mappings=_FHA_MAX_MORTGAGE_MAPPINGS,
))

_register(FormTemplate(
    template_id="va_irrrl_recoupment_calc",
    display_name="VA IRRRL Recoupment Calc",
    category="VA",
    filename="va_irrrl_recoupment_calc.xlsx",
    description="VA IRRRL recoupment comparison. Pre-fills existing P&I and loan amounts from pipeline data.",
    mappings=_VA_IRRRL_MAPPINGS,
))


# ---------------------------------------------------------------------------
# JSON path resolution
# ---------------------------------------------------------------------------

def _resolve_json_path(data: dict, dotpath: str) -> Any:
    """Traverse nested dicts/lists by dot-separated path. Returns None if missing."""
    parts = dotpath.split(".")
    current: Any = data
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


# ---------------------------------------------------------------------------
# Source data loading
# ---------------------------------------------------------------------------

def _load_source_data(profiles_dir: Path) -> dict[str, dict]:
    """Load all source JSONs into {"income_analysis": {...}, "dti": {...}, ...}."""
    result: dict[str, dict] = {}
    for source_key, (profile_name, filename) in _SOURCE_FILE_MAP.items():
        fpath = profiles_dir / profile_name / filename
        if fpath.is_file():
            try:
                with fpath.open() as f:
                    result[source_key] = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to load %s: %s", fpath, e)
                result[source_key] = {}
        else:
            result[source_key] = {}
    return result


# ---------------------------------------------------------------------------
# Fill form
# ---------------------------------------------------------------------------

def fill_form(
    template_id: str,
    profiles_dir: Path,
    output_path: Path,
    *,
    loan_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Fill a template .xlsx with pipeline data and save to output_path.

    Returns an audit dict with cells_filled, cells_total, skipped_fields.
    """
    if template_id not in FORM_TEMPLATES:
        raise ValueError(f"Unknown template_id: {template_id!r}")

    tmpl = FORM_TEMPLATES[template_id]
    template_path = _FORMS_DIR / tmpl.filename
    if not template_path.is_file():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    source_data = _load_source_data(profiles_dir)
    wb = openpyxl.load_workbook(str(template_path), data_only=False)

    cells_filled = 0
    skipped_fields: list[dict[str, str]] = []

    for mapping in tmpl.mappings:
        # Resolve source data
        src = source_data.get(mapping.source, {})
        value = _resolve_json_path(src, mapping.json_path)

        if value is None:
            skipped_fields.append({
                "cell": mapping.cell,
                "source": mapping.source,
                "json_path": mapping.json_path,
                "label": mapping.label,
                "reason": "missing",
            })
            continue

        # Select worksheet
        if mapping.sheet:
            ws = wb[mapping.sheet]
        else:
            ws = wb[wb.sheetnames[0]]

        # Coerce value to appropriate Python type
        if mapping.cell_type in (CellType.NUMBER, CellType.CURRENCY, CellType.PERCENTAGE):
            try:
                value = float(value)
            except (ValueError, TypeError):
                skipped_fields.append({
                    "cell": mapping.cell,
                    "source": mapping.source,
                    "json_path": mapping.json_path,
                    "label": mapping.label,
                    "reason": "not_numeric",
                })
                continue
        elif mapping.cell_type == CellType.TEXT:
            value = str(value)

        ws[mapping.cell] = value
        cells_filled += 1

    # Save output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))

    audit = {
        "template_id": template_id,
        "display_name": tmpl.display_name,
        "cells_filled": cells_filled,
        "cells_total": len(tmpl.mappings),
        "skipped_fields": skipped_fields,
        "loan_id": loan_id,
        "run_id": run_id,
        "output_path": str(output_path),
    }
    log.info("FormFill %s: %d/%d cells filled", template_id, cells_filled, len(tmpl.mappings))
    return audit
