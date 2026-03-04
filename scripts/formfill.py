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

_INCOME_CALC_W2_MAPPINGS = (
    # YTD Earnings
    FieldMapping("C11", "income_analysis", "monthly_income_total.value", CellType.CURRENCY,
                 label="YTD Earnings (monthly income total)"),
    # Front-end DTI
    FieldMapping("C15", "dti", "front_end_dti", CellType.PERCENTAGE,
                 label="Front-end DTI"),
    # Back-end DTI
    FieldMapping("C16", "dti", "back_end_dti", CellType.PERCENTAGE,
                 label="Back-end DTI"),
)

_FHA_MAX_MORTGAGE_MAPPINGS = (
    # Unpaid principal balance of existing FHA mortgage
    FieldMapping("I10", "income_analysis", "liability_items.0.balance", CellType.CURRENCY,
                 sheet="Simple", label="Existing mortgage UPB"),
)

_VA_IRRRL_MAPPINGS = (
    # Existing monthly P&I
    FieldMapping("B15", "income_analysis", "proposed_pitia.value", CellType.CURRENCY,
                 label="Existing monthly P&I (from PITIA)"),
    # Monthly income for context
    FieldMapping("B11", "dti", "monthly_income_total", CellType.CURRENCY,
                 label="Monthly income total"),
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
