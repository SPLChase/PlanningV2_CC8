"""Build the CoCre8 Planning V2 field map workbook."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from planning_v2.config import PlanningConfig, get_config
from planning_v2.schemas import CONFIRMED_OUTPUT_OBJECTS, FIELD_MAP_WORKBOOK, PENDING_OUTPUT_OBJECTS
from planning_v2.template_specs import load_template_fields
from planning_v2.onboarding_csvs import POPULATED_TEMPLATE_FIELDS


TARGET_FIELDS_FILE = "SPL Planning Data Fields.xlsx"

STATUS_CONFIRMED = "Confirmed"
STATUS_REVIEW = "Review required"
STATUS_INVESTIGATE_SAP = "Investigate SAP first"
STATUS_INVESTIGATE_EXTERNAL = "Investigate source"
STATUS_OUT_OF_SCOPE = "Out of v1 scope"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)
CONFIRMED_FILL = PatternFill("solid", fgColor="D9EAD3")
REVIEW_FILL = PatternFill("solid", fgColor="FCE4D6")
UNKNOWN_FILL = PatternFill("solid", fgColor="F4CCCC")
STATIC_FILL = PatternFill("solid", fgColor="E7E6E6")
TABLE_STYLE = TableStyleInfo(
    name="TableStyleMedium2",
    showFirstColumn=False,
    showLastColumn=False,
    showRowStripes=True,
    showColumnStripes=False,
)


@dataclass(frozen=True)
class TargetField:
    row_number: int
    field_name: str
    object_name: str
    context_area: str
    key_column: str
    used_by_spl: str
    data_type: str
    explanation: str
    cc8_relevant: str
    cc8_can_supply: str


@dataclass(frozen=True)
class FieldDecision:
    target: TargetField
    source_status: str
    source_system: str
    source_object: str
    source_field: str
    transform: str
    confidence: str
    notes: str


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").strip()
    if text.lower() in {"none", "nan", "null", "<na>"}:
        return ""
    return text


def normalize_name(value: object) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def parse_target_fields(reference_dir: Path) -> list[TargetField]:
    path = reference_dir / TARGET_FIELDS_FILE
    if not path.exists():
        raise FileNotFoundError(f"Missing target field workbook: {path}")

    workbook = load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook.active
    headers = [clean_text(cell) for cell in next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True))]
    index = {header: i for i, header in enumerate(headers) if header}
    required = [
        "Field name",
        "Key column",
        "Used by SPL",
        "Data Type Info",
        "Field Explanation",
        "Context Area",
        "Altron file",
        "CC8 relevant?",
        "CC8 can supply?",
    ]
    missing = [name for name in required if name not in index]
    if missing:
        raise ValueError(f"Missing columns in {path}: {', '.join(missing)}")

    fields: list[TargetField] = []
    for row_number, row in enumerate(worksheet.iter_rows(min_row=2, values_only=True), start=2):
        field_name = clean_text(row[index["Field name"]])
        if not field_name:
            continue
        fields.append(
            TargetField(
                row_number=row_number,
                field_name=field_name,
                object_name=clean_text(row[index["Altron file"]]),
                context_area=clean_text(row[index["Context Area"]]),
                key_column=clean_text(row[index["Key column"]]),
                used_by_spl=clean_text(row[index["Used by SPL"]]),
                data_type=clean_text(row[index["Data Type Info"]]),
                explanation=clean_text(row[index["Field Explanation"]]),
                cc8_relevant=clean_text(row[index["CC8 relevant?"]]),
                cc8_can_supply=clean_text(row[index["CC8 can supply?"]]),
            )
        )
    workbook.close()
    return fields


def is_v1_target(field: TargetField) -> bool:
    relevance = field.cc8_relevant.lower()
    if relevance.startswith("yes") or relevance.startswith("maybe"):
        return True
    if field.context_area in {"Stock detail", "warehouse details"}:
        return field.used_by_spl.upper() == "YES"
    if field.context_area in {"purchase_orders", "repair_orders"}:
        return field.used_by_spl.upper() == "YES"
    return False


def classify_field(field: TargetField) -> FieldDecision:
    object_key = normalize_name(field.object_name)
    field_key = normalize_name(field.field_name)
    supplied = field.cc8_can_supply.lower()
    context = field.context_area

    confirmed_sources = {
        "ams_masters_spl_generated_from_distribution_portal": ("Local file", "masters.csv", "SPL Master / Items linked"),
        "bpart_alt": ("Local file", "masters.csv", "Items linked"),
        "bpart_cost": ("SPI file", "SPI_DATA.csv", field.field_name),
        "whse_bpart_qty": ("SAP + Exco", "OITW / InventoryCurrent.csv", field.field_name),
        "warehouse": ("Local dimension", "warehouse_dimension.csv / FactWarehouses.csv", field.field_name),
        "cust": ("Local dimension", "customers.csv / warehouse_dimension.csv", field.field_name),
    }
    if object_key in confirmed_sources:
        source_system, source_object, source_field = confirmed_sources[object_key]
        return FieldDecision(
            target=field,
            source_status=STATUS_CONFIRMED,
            source_system=source_system,
            source_object=source_object,
            source_field=source_field,
            transform=_default_transform(field),
            confidence="High" if object_key != "bpart_cost" else "Medium",
            notes="Confirmed from MinStock3/Exco local sources; review business meaning where noted.",
        )

    if object_key == "bpart":
        if field_key in {"abc_id", "is_exclude_from_replishment", "cst_branch_stockable", "cst_boot_stock"}:
            return _review_decision(field, "Local/SPI-derived item attributes need business confirmation.")
        if field_key in {"part_id", "pclass2_id", "pclass3_id"}:
            return FieldDecision(field, STATUS_CONFIRMED, "SAP/SPI/local", "OITM / SPI_DATA.csv", field.field_name, _default_transform(field), "Medium", "Part identity and class-like attributes are present, but exact V2 semantic mapping needs review.")
        return _sap_investigation_decision(field, "SAP item master field candidate; exact source column not yet proven.")

    if object_key in {"goods", "ib_warehouse", "model"}:
        return _sap_investigation_decision(field, "Installed base/model/BOM source not proven for CoCre8.")

    if object_key == "imo":
        return _sap_investigation_decision(field, "Inventory transfer or stock transfer request source must be verified in SAP.")

    if object_key == "order_line":
        return _sap_investigation_decision(field, "Marked Power Automate in reference, but plan requires SAP-first investigation.")

    if object_key == "parts_usage":
        if supplied.startswith("yes"):
            return FieldDecision(field, STATUS_CONFIRMED, "Stock audit / Exco Usage", "Stock Audit Report / Usage.csv", field.field_name, _default_transform(field), "Medium", "Usage is confirmed at item/warehouse/date grain; work-order linkage may need SAP verification.")
        return _sap_investigation_decision(field, "Usage detail exists, but serial/date granularity needs investigation.")

    if object_key in {"person", "node", "demand_trans_log"}:
        return _sap_investigation_decision(field, "Reference marks this as possible but source is not proven.")

    if object_key in {"multiple", "critical_parts", "exclusions_import_template_spl_generated_by_distribution_portal"}:
        return FieldDecision(field, STATUS_INVESTIGATE_EXTERNAL, "SPL distribution portal/manual rule", field.object_name, field.field_name, "Import as maintained rule table once provided.", "Low", "Not available from MinStock3/Exco evidence.")

    if object_key in {"purchase_orders", "repair_orders", "service_call_env"}:
        return _sap_investigation_decision(field, "Important V2 vision object; not confirmed in current CC8 local sources.")

    if supplied.startswith("yes"):
        return FieldDecision(field, STATUS_REVIEW, "Reference workbook", field.object_name, field.field_name, _default_transform(field), "Medium", "Reference says CC8 can supply, but no local proof was identified.")
    if "power automate" in supplied:
        return _sap_investigation_decision(field, "External hint exists, but SAP-first investigation is required.")
    if is_v1_target(field):
        return _sap_investigation_decision(field, "V1 target field has no confirmed source.")
    return FieldDecision(field, STATUS_OUT_OF_SCOPE, "", "", "", "", "", "Not marked CC8 relevant for v1.")


def _review_decision(field: TargetField, notes: str) -> FieldDecision:
    return FieldDecision(field, STATUS_REVIEW, "Local/SPI/SPL rule", field.object_name, field.field_name, _default_transform(field), "Medium", notes)


def _sap_investigation_decision(field: TargetField, notes: str) -> FieldDecision:
    return FieldDecision(field, STATUS_INVESTIGATE_SAP, "SAP Service Layer", field.object_name, field.field_name, "Do not populate until SAP source is proven.", "Low", notes)


def _default_transform(field: TargetField) -> str:
    key = normalize_name(field.field_name)
    if key in {"primary_part"}:
        return "Use first linked item from SPL master mapping; review required."
    if "cost" in key or "price" in key:
        return "Coerce numeric currency value; preserve original currency where available."
    if key in {"qty", "quantity", "qty_used", "allocated_qty", "in_bound_qty", "out_bound_qty"}:
        return "Coerce to numeric quantity."
    if "date" in key or "time" in key or key.endswith("_at"):
        return "Parse to ISO datetime/date where source supports it."
    return "Direct map after trimming text."


def build_decisions(fields: Iterable[TargetField]) -> list[FieldDecision]:
    return [classify_field(field) for field in fields if is_v1_target(field)]


def source_evidence_rows(cfg: PlanningConfig) -> list[dict[str, str]]:
    candidates = [
        ("MinStock3 SAP stock", cfg.minstock3_dir / "extract_raw_stock.py", "OITW/OITM SQL for item, warehouse, on hand, minimum, ordered."),
        ("MinStock3 open PO", cfg.minstock3_dir / "extract_raw_stock.py", "OPOR/POR1 SQL for recent open purchase order quantity."),
        ("MinStock3 SPL masters", cfg.minstock3_dir / "map_masters.py", "masters.csv maps linked items to SPLMaster/MasterKey."),
        ("MinStock3 warehouse/customer", cfg.minstock3_dir / "enrich_dimensions.py", "FactWarehouses.csv and customers.csv enrich warehouse/customer dimensions."),
        ("MinStock3 SPI", cfg.minstock3_dir / "enrich_dimensions.py", "SPI_DATA supplies return/repair/list/credit pricing and derived CoCre8 cost."),
        ("MinStock3 usage", cfg.minstock3_dir / "enrich_dimensions.py", "Stock audit DN rows aggregate annual usage."),
        ("Exco inventory output", cfg.exco_output_dir / "InventoryCurrent.csv", "Current stock, warehouse/customer, item, price, bin, and cost fields."),
        ("Exco usage output", cfg.exco_output_dir / "Usage.csv", "Usage fact by posting date, item, warehouse, customer, and quantity."),
        ("Exco stock flow output", cfg.exco_output_dir / "StockFlow.csv", "All stock audit movements by document type and signed quantity."),
        ("Reference target fields", cfg.reference_dir / TARGET_FIELDS_FILE, "Planning V2 target field metadata and CC8 relevance hints."),
        ("Planning V2 sample templates", cfg.samples_dir / "Templates raw.xlsx", "Canonical onboarding template fields used for template-shaped CSV outputs."),
    ]
    rows = []
    for source_name, path, evidence in candidates:
        rows.append(
            {
                "Source": source_name,
                "Path": str(path),
                "Exists": "Yes" if path.exists() else "No",
                "Evidence": evidence,
            }
        )
    return rows


def output_object_rows(decisions: Iterable[FieldDecision]) -> list[dict[str, str]]:
    by_object: dict[str, list[FieldDecision]] = {}
    for decision in decisions:
        key = normalize_name(decision.target.object_name or decision.target.context_area)
        by_object.setdefault(key, []).append(decision)

    rows: list[dict[str, str]] = []
    for object_name, fields in CONFIRMED_OUTPUT_OBJECTS.items():
        blockers = sorted(
            {
                decision.target.field_name
                for decision in by_object.get(normalize_name(object_name), [])
                if decision.source_status in {STATUS_INVESTIGATE_SAP, STATUS_INVESTIGATE_EXTERNAL}
            }
        )
        rows.append(
            {
                "Object": object_name,
                "CSV": f"{object_name}.csv",
                "Readiness": "Populated" if not blockers else "Partial",
                "Fields": ", ".join(fields),
                "Blockers": "; ".join(blockers),
            }
        )
    for object_name, fields in PENDING_OUTPUT_OBJECTS.items():
        rows.append(
            {
                "Object": object_name,
                "CSV": f"{object_name}.csv",
                "Readiness": "Pending/header only",
                "Fields": ", ".join(fields),
                "Blockers": "SAP source not proven for v1.",
            }
        )
    return rows


def sap_investigation_rows(decisions: Iterable[FieldDecision]) -> list[dict[str, str]]:
    rows = []
    seen: set[tuple[str, str]] = set()
    candidates = {
        "order_line": ("ServiceCalls / Orders / SQLQueries", "work orders, customer/site, serial, open/resolve timestamps"),
        "service_call_env": ("ServiceCalls / Activity endpoints / SQLQueries", "ETA, recall, resolve, SLA clocks and failure codes"),
        "imo": ("InventoryTransferRequests / StockTransfers / SQLQueries", "from/to warehouse, status, dates, quantities, linked order"),
        "purchase_orders": ("PurchaseOrders / OPOR/POR1 / GoodsReceiptPO", "PO line, vendor, quantity, GRN/receipt status"),
        "repair_orders": ("ServiceCalls / inventory transfers / user tables", "repair order, return authorization, repair status and ETA"),
        "ib_warehouse": ("EquipmentCards / SerialNumbers / BillOfMaterials", "installed base, model, serial, site, BOM quantity"),
        "goods": ("Items / ProductTrees", "model-to-part/BOM relationship"),
        "model": ("Items / ProductTrees", "model ids and descriptions"),
        "person": ("EmployeesInfo / Contacts / SalesPersons", "technician identity and active status"),
        "node": ("Warehouses / Branches / user tables", "warehouse hierarchy and regional node"),
    }
    for decision in decisions:
        object_key = normalize_name(decision.target.object_name)
        if decision.source_status != STATUS_INVESTIGATE_SAP or object_key not in candidates:
            continue
        endpoint, purpose = candidates[object_key]
        key = (object_key, endpoint)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "Object": decision.target.object_name,
                "SAP Candidate": endpoint,
                "Purpose": purpose,
                "Status": "Not run",
                "Decision": "Blocked until query/endpoint is proven against CoCre8 SAP.",
            }
        )
    return rows


def unknown_rows(decisions: Iterable[FieldDecision]) -> list[dict[str, str]]:
    rows = []
    for decision in decisions:
        if decision.source_status not in {STATUS_INVESTIGATE_SAP, STATUS_INVESTIGATE_EXTERNAL, STATUS_REVIEW}:
            continue
        rows.append(
            {
                "Field": decision.target.field_name,
                "Object": decision.target.object_name,
                "Context": decision.target.context_area,
                "Status": decision.source_status,
                "Question": _investigation_question(decision),
                "Next Action": _next_action(decision),
                "Notes": decision.notes,
            }
        )
    return rows


def _investigation_question(decision: FieldDecision) -> str:
    field = decision.target
    if decision.source_status == STATUS_REVIEW:
        return f"Does {field.field_name} mean the same thing for CoCre8 as in the SPL reference workbook?"
    if decision.source_status == STATUS_INVESTIGATE_EXTERNAL:
        return f"Who owns the manual/distribution-portal source for {field.object_name}.{field.field_name}?"
    return f"Which SAP Service Layer endpoint/table supplies {field.object_name}.{field.field_name} for CoCre8?"


def _next_action(decision: FieldDecision) -> str:
    if decision.source_status == STATUS_REVIEW:
        return "Review with business owner before enabling as populated output."
    if decision.source_status == STATUS_INVESTIGATE_EXTERNAL:
        return "Request source file/rule owner, then add parser once received."
    return "Run SAP metadata/query investigation and record result in SAP Investigation Log."


def build_field_map_workbook(cfg: PlanningConfig, output_path: Path | None = None) -> Path:
    fields = parse_target_fields(cfg.reference_dir)
    decisions = build_decisions(fields)
    output_path = output_path or Path("docs") / "field-map" / FIELD_MAP_WORKBOOK
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    workbook.remove(workbook.active)
    _write_sheet(workbook, "Target Fields", _target_field_rows(decisions))
    _write_sheet(workbook, "Template Fields", template_field_rows(cfg))
    _write_sheet(workbook, "Source Evidence", source_evidence_rows(cfg))
    _write_sheet(workbook, "Unknowns", unknown_rows(decisions) + template_unknown_rows(cfg))
    _write_sheet(workbook, "Output Objects", output_object_rows(decisions) + template_output_rows(cfg))
    _write_sheet(workbook, "SAP Investigation Log", sap_investigation_rows(decisions))
    workbook.save(output_path)
    return output_path


def template_field_rows(cfg: PlanningConfig) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for object_name, fields in load_template_fields(cfg.samples_dir).items():
        populated = POPULATED_TEMPLATE_FIELDS.get(object_name, {})
        for field in fields:
            source = populated.get(field.field_name, "")
            if source:
                status = STATUS_CONFIRMED
                confidence = "High" if field.field_name not in {"uniqueId"} else "Medium"
                notes = "Populated in template CSV from confirmed local CoCre8/Exco source."
            else:
                status = STATUS_INVESTIGATE_SAP
                confidence = "Low"
                notes = "Left blank in generated template CSV until source and semantics are confirmed."
            rows.append(
                {
                    "Template Object": object_name,
                    "Field": field.field_name,
                    "Field Type": field.field_type,
                    "Template Notes": field.notes,
                    "Source Status": status,
                    "Source": source,
                    "Confidence": confidence,
                    "Implementation Notes": notes,
                }
            )
    return rows


def template_unknown_rows(cfg: PlanningConfig) -> list[dict[str, str]]:
    rows = []
    for row in template_field_rows(cfg):
        if row["Source Status"] == STATUS_CONFIRMED:
            continue
        rows.append(
            {
                "Field": row["Field"],
                "Object": row["Template Object"],
                "Context": "Planning V2 template",
                "Status": row["Source Status"],
                "Question": f"What confirmed CoCre8/SAP source should populate {row['Template Object']}.{row['Field']}?",
                "Next Action": "Investigate SAP Service Layer first; only use external/manual source if SAP cannot supply it.",
                "Notes": row["Implementation Notes"],
            }
        )
    return rows


def template_output_rows(cfg: PlanningConfig) -> list[dict[str, str]]:
    rows = []
    for object_name, fields in load_template_fields(cfg.samples_dir).items():
        populated = POPULATED_TEMPLATE_FIELDS.get(object_name, {})
        readiness = "Partial" if populated else "Pending/header only"
        blockers = [field.field_name for field in fields if field.field_name not in populated]
        rows.append(
            {
                "Object": object_name,
                "CSV": f"{object_name}.csv",
                "Readiness": readiness,
                "Fields": ", ".join(field.field_name for field in fields),
                "Blockers": "; ".join(blockers),
            }
        )
    return rows


def _target_field_rows(decisions: Iterable[FieldDecision]) -> list[dict[str, str]]:
    rows = []
    for decision in decisions:
        field = decision.target
        rows.append(
            {
                "Reference Row": str(field.row_number),
                "Field": field.field_name,
                "Object": field.object_name,
                "Context": field.context_area,
                "Used by SPL": field.used_by_spl,
                "CC8 Relevant": field.cc8_relevant,
                "CC8 Supply Hint": field.cc8_can_supply,
                "Data Type": field.data_type,
                "Source Status": decision.source_status,
                "Source System": decision.source_system,
                "Source Object": decision.source_object,
                "Source Field": decision.source_field,
                "Transform": decision.transform,
                "Confidence": decision.confidence,
                "Explanation": field.explanation,
                "Notes": decision.notes,
            }
        )
    return rows


def _write_sheet(workbook: Workbook, title: str, rows: list[dict[str, str]]) -> None:
    worksheet = workbook.create_sheet(title)
    headers = list(rows[0].keys()) if rows else ["Note"]
    if not rows:
        rows = [{"Note": "No rows"}]

    worksheet.append(headers)
    for row in rows:
        worksheet.append([row.get(header, "") for header in headers])

    for cell in worksheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.sheet_view.showGridLines = False

    for row in worksheet.iter_rows(min_row=2):
        status = ""
        for cell in row:
            if worksheet.cell(row=1, column=cell.column).value in {"Source Status", "Status", "Readiness"}:
                status = clean_text(cell.value)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        fill = _status_fill(status)
        if fill:
            for cell in row:
                cell.fill = fill

    table_ref = f"A1:{get_column_letter(worksheet.max_column)}{worksheet.max_row}"
    table = Table(displayName=re.sub(r"[^A-Za-z0-9_]", "", title)[:25] or "Table", ref=table_ref)
    table.tableStyleInfo = TABLE_STYLE
    worksheet.add_table(table)

    for column_cells in worksheet.columns:
        header = clean_text(column_cells[0].value)
        width = min(max(len(header), *(len(clean_text(cell.value)) for cell in column_cells[1:]), 12), 60)
        worksheet.column_dimensions[get_column_letter(column_cells[0].column)].width = width + 2


def _status_fill(status: str) -> PatternFill | None:
    if status in {STATUS_CONFIRMED, "Populated"}:
        return CONFIRMED_FILL
    if status in {STATUS_REVIEW, "Partial"}:
        return REVIEW_FILL
    if status in {STATUS_INVESTIGATE_SAP, STATUS_INVESTIGATE_EXTERNAL, "Pending/header only"}:
        return UNKNOWN_FILL
    if status == STATUS_OUT_OF_SCOPE:
        return STATIC_FILL
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the CoCre8 Planning V2 field map workbook.")
    parser.add_argument("--reference-dir", type=Path, default=None)
    parser.add_argument("--cc8-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("docs") / "field-map" / FIELD_MAP_WORKBOOK)
    args = parser.parse_args(argv)

    cfg = get_config()
    if args.reference_dir:
        cfg = PlanningConfig(**{**cfg.__dict__, "reference_dir": args.reference_dir})
    if args.cc8_root:
        cfg = PlanningConfig(
            **{
                **cfg.__dict__,
                "cc8_source_root": args.cc8_root,
                "exco_source_dir": args.cc8_root / "Exco" / "source_data",
                "exco_output_dir": args.cc8_root / "Exco" / "output" / "csv",
                "minstock3_dir": args.cc8_root / "MinStock3",
            }
        )
    path = build_field_map_workbook(cfg, args.out)
    print(f"Wrote field map workbook: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
