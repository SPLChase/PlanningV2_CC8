"""Generate confirmed Planning V2 onboarding CSVs from live CoCre8 SAP sources."""

from __future__ import annotations

import argparse
import re
import zlib
from datetime import date
from pathlib import Path

import pandas as pd

from planning_v2.config import PlanningConfig, get_config
from planning_v2.issue_tracker import (
    issue_tracker_evidence_rows,
    master_lookup,
    part_key,
    purchase_order_reconciliation,
    purchase_order_ticket_matches,
    read_issue_tracker,
)
from planning_v2.schemas import CONFIRMED_OUTPUT_OBJECTS, PENDING_OUTPUT_OBJECTS
from planning_v2.sap_extracts import fetch_live_delivery_note_usage, fetch_live_purchase_orders, fetch_live_template_sources
from planning_v2.template_specs import template_columns


POPULATED_TEMPLATE_FIELDS = {
    "Parts": {
        "SPLMaster": "Reference masters.csv:SPL Master by linked item",
        "PartNumber": "SAP Service Layer SQLQueries:OITM.ItemCode",
        "isPrimary": "SPI_DATA.csv:Main alternative par equals material/part number; defaults true when no main alternative is listed",
        "primaryPartNumber": "SPI_DATA.csv:Main alternative par; defaults to own part number when no main alternative is listed",
        "description": "SAP Service Layer SQLQueries:OITM.ItemName",
        "isBootStockable": "Business rule: N for all CoCre8 stock",
        "isBranchStockable": "Business rule: Y for all CoCre8 stock",
        "isObsolete": "Business rule: N for all CoCre8 stock for now",
        "isExcludeFromReplenishment": "Business rule: N for all CoCre8 stock for now",
        "purchaseLeadTimeDays": "Business rule: default 3 days; 180 days when description contains BBU",
    },
    "Warehouses": {
        "warehouseId": "SAP Service Layer Warehouses:WarehouseCode",
        "addressId": "Manual fill workbook:addressId; currently warehouse code per user mapping",
        "returnWarehouseId": "Manual fill workbook:returnWarehouseId",
        "supplyWarehouseId": "Manual fill workbook:supplyWarehouseId",
        "warehouseTypeId": "Manual fill workbook:warehouseTypeId",
        "warehouseDescription": "SAP Service Layer Warehouses:WarehouseName",
        "isReplenishable": "Manual fill workbook:isReplenishable",
        "isBranchStockable": "Manual fill workbook:isBranchStockable",
        "isRemote": "Manual fill workbook:isRemote",
        "warehouseStatusId": "Manual fill workbook:isObsolete inverted to is_active flag (Y active, N obsolete)",
    },
    "WarehouseStockOnHand": {
        "partCode": "SAP Service Layer SQLQueries:OITW.ItemCode",
        "warehouseCode": "SAP Service Layer SQLQueries:OITW.WhsCode",
        "quantityAllocated": "SAP Service Layer SQLQueries:OITW.IsCommited",
        "quantityOnHand": "SAP Service Layer SQLQueries:OITW.OnHand",
        "quantityInbound": "SAP Service Layer SQLQueries:OITW.OnOrder",
        "uniqueId": "Derived stable integer from MinStock-style RowKey part|SPLMaster|warehouse",
    },
    "Vendors": {
        "vendorId": "SAP Service Layer SQLQueries:OPOR.CardCode",
        "Description": "SAP Service Layer SQLQueries:OCRD.CardName joined from PO vendor",
        "isActive": "SAP Service Layer SQLQueries:OCRD.validFor joined from PO vendor",
    },
    "Customers": {},
    "PartsUsage": {
        "orderNumber": "SAP Service Layer SQLQueries:ODLN.NumAtCard or parsed Call Nr from ODLN.Comments",
        "requestId": "SAP Service Layer SQLQueries:ODLN.NumAtCard or parsed Call Nr from ODLN.Comments",
        "customerCompanyCode": "SAP Service Layer SQLQueries:parsed Customer from ODLN.Comments where present",
        "orderStartDatetime": "HelpDesk issue tracker Created date by unambiguous call-number match",
        "orderStatus": "HelpDesk issue tracker Status by unambiguous call-number match",
        "partCode": "SAP Service Layer SQLQueries:DLN1.ItemCode actual delivered part",
        "serialNumber": "SAP Service Layer SQLQueries:parsed Serial number from ODLN.Comments where present",
        "quantityUsed": "SAP Service Layer SQLQueries:DLN1.Quantity",
        "partsUsedDateTime": "SAP Service Layer SQLQueries:ODLN.DocDate",
        "Warehouse": "SAP Service Layer SQLQueries:DLN1.WhsCode",
        "deviceSerialNumber": "SAP Service Layer SQLQueries:parsed Serial number from ODLN.Comments where present",
        "Master": "Reference masters.csv:SPL Master by used part",
    },
    "PurchaseOrders": {
        "purchaseOrderNumber": "SAP Service Layer SQLQueries:OPOR.NumAtCard, falling back only to PO-like reference parsed from OPOR.Comments",
        "purchaseOrderStatus": "SAP Service Layer SQLQueries:OPOR.DocStatus/CANCELED mapped to template status",
        "creationDateTime": "SAP Service Layer SQLQueries:OPOR.CreateDate",
        "approvalDateTime": "SAP Service Layer SQLQueries:OPOR.DocDate",
        "toWarehouseId": "SAP Service Layer SQLQueries:POR1.WhsCode",
        "vendorId": "SAP Service Layer SQLQueries:OPOR.CardCode",
        "partNumber": "SAP Service Layer SQLQueries:POR1.ItemCode",
        "quantity": "SAP Service Layer SQLQueries:POR1.Quantity",
        "lineCost": "Current SPI_DATA.csv then Reference/SPI_Historical newest-first:ListPrice multiplied by 0.72 CoCre8 cost factor",
        "quantityReceived": "SAP Service Layer SQLQueries:PDN1.Quantity summed by PO line",
        "receivedDateTime": "SAP Service Layer SQLQueries:OPDN.DocDate max by PO line",
    },
    "InventoryTransfers": {
        "inventoryTransferImoId": "Stock Audit Report.txt:Document + Item No. + absolute Quantity + pair sequence for IM rows",
        "createdDateTime": "Stock Audit Report.txt:Posting Date for paired IM rows",
        "completedDateTime": "Stock Audit Report.txt:Posting Date for paired IM rows",
        "fromWarehouseId": "Stock Audit Report.txt:Whse on negative IM quantity row",
        "toWarehouseId": "Stock Audit Report.txt:Whse on positive IM quantity row",
        "demandStatus": "Business rule: Fulfilled for posted paired IM rows",
        "orderStatusIsClosed": "Business rule: Y for posted paired IM rows",
        "isResolved": "Business rule: Y for posted paired IM rows",
        "partNumber": "Stock Audit Report.txt:forward-filled Item No. for IM rows",
        "quantity": "Stock Audit Report.txt:absolute IM Quantity for paired rows",
    },
    "PartCost": {
        "partCode": "Actual SAP/Fujitsu part id from live SAP parts and PO lines",
        "cost": "Current SPI_DATA.csv then Reference/SPI_Historical newest-first:ListPrice multiplied by 0.72, selected as of last PO month where possible",
        "currencyCode": "Business rule: EUR for SPI CoCre8 costs",
        "averageCost": "PO quantity-weighted average of SPI-derived CoCre8 costs across SAP PO history",
    },
}

OUT_OF_SCOPE_TEMPLATE_FIELDS = {
    ("ActionGroups", "actionGroupId"),
    ("ActionGroups", "nodeId"),
    ("ActionGroups", "actionGroupDescription"),
    ("ActionGroups", "assignAnySkill"),
    ("ActionGroups", "isUsed"),
    ("ActionGroups", "isObsolete"),
    ("Employees", "actionGroupId"),
    ("InventoryTransfers", "loClass"),
    ("PartCost", "averageRepairCost"),
    ("PartsUsage", "resolvedDateTime"),
    ("PartsUsage", "relCompanyId"),
    ("PartsUsage", "assignedPersonCode"),
    ("Parts", "isService"),
    ("Parts", "isTool"),
    ("Parts", "isSmallPart"),
    ("PurchaseOrders", "customerId"),
    ("PurchaseOrders", "requestTicketDateTime"),
    ("PurchaseOrders", "isResolved"),
    ("InventoryTransfers", "addressId"),
    ("Warehouses", "nodeId"),
    ("Warehouses", "isRepairWarehouse"),
    ("Warehouses", "isBootStockable"),
    ("WarehouseStockOnHand", "inventoryType"),
    ("WarehouseStockOnHand", "quantityOutbound"),
}

ROW_REQUIRED_TEMPLATE_FIELDS = {
    "PartCost": ["partCode", "cost", "currencyCode", "averageCost"],
}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def _write_csv(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0.0)


def _col(df: pd.DataFrame, name: str, default: object = "") -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index)


def _first_col(df: pd.DataFrame, names: list[str], default: object = "") -> pd.Series:
    for name in names:
        if name in df.columns:
            return df[name]
    return pd.Series([default] * len(df), index=df.index)


def _first_existing(*frames: pd.DataFrame) -> pd.DataFrame:
    for frame in frames:
        if not frame.empty:
            return frame
    return pd.DataFrame()


def generate_onboarding_csvs(cfg: PlanningConfig, out_dir: Path) -> list[Path]:
    csv_dir = out_dir
    template_dir = out_dir.parent / "investigation_templates"
    csv_dir.mkdir(parents=True, exist_ok=True)
    template_dir.mkdir(parents=True, exist_ok=True)

    parts, warehouses, inventory = fetch_live_template_sources(cfg)
    purchase_orders = fetch_live_purchase_orders(cfg)
    usage = fetch_live_delivery_note_usage(cfg)
    stock_audit = _read_stock_audit(_stock_audit_3y_path(cfg))
    if usage.empty:
        usage = stock_audit
    stock_flow = stock_audit
    customers = pd.DataFrame()
    masters = _read_masters(cfg.reference_dir / "masters.csv")
    spi = _read_spi(cfg.exco_source_dir / "SPI_DATA.csv")
    spi_cost_history = _read_spi_cost_history(cfg.reference_dir / "SPI_Historical")
    combined_spi_costs = _combine_spi_cost_sources(spi, spi_cost_history)
    issue_tracker = read_issue_tracker(cfg.issue_tracker_csv)
    manual_warehouses = read_manual_warehouse_fill(cfg)

    written: list[Path] = []
    templates = template_columns(cfg.samples_dir)
    outputs = build_template_outputs(
        templates,
        inventory,
        usage,
        stock_flow,
        warehouses,
        customers,
        parts,
        spi,
        masters,
        purchase_orders,
        issue_tracker,
        manual_warehouses,
        spi_cost_history,
    )

    for object_name, df in outputs.items():
        written.append(_write_csv(df, csv_dir / f"{object_name}.csv"))

    validation = validate_template_outputs(csv_dir, outputs, templates)
    written.append(_write_csv(validation, out_dir.parent / "validation_summary.csv"))
    written.extend(
        write_review_evidence(
            out_dir.parent / "review_evidence",
            usage,
            purchase_orders,
            issue_tracker,
            masters,
            manual_warehouses,
            parts,
            combined_spi_costs,
        )
    )
    return written


def build_template_outputs(
    templates: dict[str, list[str]],
    inventory: pd.DataFrame,
    usage: pd.DataFrame,
    stock_flow: pd.DataFrame,
    warehouses: pd.DataFrame,
    customers: pd.DataFrame,
    parts: pd.DataFrame,
    spi: pd.DataFrame,
    masters: pd.DataFrame | None = None,
    purchase_orders: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
    manual_warehouses: pd.DataFrame | None = None,
    spi_cost_history: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    outputs: dict[str, pd.DataFrame] = {}
    for object_name, columns in templates.items():
        if object_name == "Parts":
            outputs[object_name] = build_template_parts(parts, columns, spi, masters)
        elif object_name == "Warehouses":
            outputs[object_name] = build_template_warehouses(warehouses, columns, manual_warehouses)
        elif object_name == "WarehouseStockOnHand":
            outputs[object_name] = build_template_stock_on_hand(inventory, columns, masters)
        elif object_name == "Customers":
            outputs[object_name] = build_template_customers(customers, columns)
        elif object_name == "Vendors":
            outputs[object_name] = build_template_vendors(
                purchase_orders if purchase_orders is not None else pd.DataFrame(),
                columns,
            )
        elif object_name == "PartsUsage":
            outputs[object_name] = build_template_parts_usage(usage, columns, masters, issue_tracker)
        elif object_name == "PurchaseOrders":
            combined_spi = _combine_spi_cost_sources(spi, spi_cost_history if spi_cost_history is not None else pd.DataFrame())
            outputs[object_name] = build_template_purchase_orders(
                purchase_orders if purchase_orders is not None else pd.DataFrame(),
                columns,
                masters,
                issue_tracker,
                combined_spi,
            )
        elif object_name == "PartCost":
            combined_spi = _combine_spi_cost_sources(spi, spi_cost_history if spi_cost_history is not None else pd.DataFrame())
            outputs[object_name] = build_template_part_cost(
                parts,
                purchase_orders if purchase_orders is not None else pd.DataFrame(),
                columns,
                combined_spi,
            )
        elif object_name == "InventoryTransfers":
            outputs[object_name] = build_template_inventory_transfers(stock_flow, columns)
        else:
            outputs[object_name] = pd.DataFrame(columns=columns)
    return outputs


def _blank_template(columns: list[str], length: int) -> pd.DataFrame:
    return pd.DataFrame({column: [""] * length for column in columns})


def _read_spi(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def _combine_spi_cost_sources(current_spi: pd.DataFrame, historical_spi: pd.DataFrame) -> pd.DataFrame:
    current = current_spi.copy() if current_spi is not None and not current_spi.empty else pd.DataFrame()
    if not current.empty:
        if "SourceDate" not in current.columns:
            current["SourceDate"] = date.today().isoformat()
        else:
            current["SourceDate"] = current["SourceDate"].where(
                current["SourceDate"].astype(str).str.strip().ne(""),
                date.today().isoformat(),
            )
        if "SourceFile" not in current.columns:
            current["SourceFile"] = "SPI_DATA.csv"
        else:
            current["SourceFile"] = current["SourceFile"].where(
                current["SourceFile"].astype(str).str.strip().ne(""),
                "SPI_DATA.csv",
            )
    frames = [frame for frame in [current, historical_spi] if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False).fillna("")


def _read_spi_cost_history(history_dir: Path) -> pd.DataFrame:
    if not history_dir.exists():
        return pd.DataFrame(columns=["Material", "PartNumber", "ListPrice", "SourceDate", "SourceFile"])

    frames: list[pd.DataFrame] = []
    for path in sorted(history_dir.glob("*"), key=_spi_history_sort_key, reverse=True):
        if path.suffix.lower() == ".csv":
            frame = _read_spi_cost_csv(path)
        elif path.suffix.lower() in {".xlsx", ".xlsm"}:
            frame = _read_spi_cost_workbook(path)
        else:
            continue
        if frame.empty:
            continue
        source_date = _spi_history_date(path)
        frame["SourceDate"] = source_date.isoformat() if source_date else ""
        frame["SourceFile"] = path.name
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["Material", "PartNumber", "ListPrice", "SourceDate", "SourceFile"])
    return pd.concat(frames, ignore_index=True, sort=False).fillna("")


def _read_spi_cost_csv(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    except UnicodeDecodeError:
        frame = pd.read_csv(path, sep=";", dtype=str, encoding="cp1252").fillna("")
    return _normalise_spi_cost_columns(frame)


def _read_spi_cost_workbook(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_excel(path, sheet_name="Spareparts", dtype=str).fillna("")
    except ValueError:
        return pd.DataFrame(columns=["Material", "PartNumber", "ListPrice"])
    return _normalise_spi_cost_columns(frame)


def _normalise_spi_cost_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["Material", "PartNumber", "ListPrice"])
    aliases = {
        "Material": ["Material", "Material number"],
        "PartNumber": ["PartNumber", "Part number"],
        "ListPrice": ["ListPrice", "List price"],
    }
    out = pd.DataFrame(index=frame.index)
    for output, candidates in aliases.items():
        source = next((column for column in candidates if column in frame.columns), None)
        out[output] = frame[source] if source else ""
    return out[["Material", "PartNumber", "ListPrice"]].fillna("")


def _spi_history_date(path: Path) -> date | None:
    name = path.stem
    numeric = re.search(r"(20\d{6})", name)
    if numeric:
        parsed = pd.to_datetime(numeric.group(1), format="%Y%m%d", errors="coerce")
        if not pd.isna(parsed):
            return parsed.date()
    month = re.search(r"_(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{2})$", name, flags=re.IGNORECASE)
    if month:
        parsed = pd.to_datetime(f"01 {month.group(1)} 20{month.group(2)}", format="%d %b %Y", errors="coerce")
        if not pd.isna(parsed):
            return parsed.date()
    return None


def _spi_history_sort_key(path: Path) -> tuple[date, str]:
    return (_spi_history_date(path) or date.min, path.name)


def _read_masters(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["SPL Master", "Items linked"])
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def _stock_audit_3y_path(cfg: PlanningConfig) -> Path:
    candidates = [
        cfg.reference_dir / "Stock Audit Report.txt",
        cfg.exco_source_dir / "Stock Audit Report 3Y.txt",
        cfg.cc8_source_root / "Exco" / "April_refresh" / "Stock Audit Report 3Y.txt",
        cfg.exco_source_dir / "Stock Audit Report.txt",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def read_manual_warehouse_fill(cfg: PlanningConfig) -> pd.DataFrame:
    candidates = [
        Path("docs") / "manual-fill" / "Warehouses_Missing_Fields_To_Fill_simple.xlsx",
        Path("docs") / "manual-fill" / "Warehouses_Missing_Fields_To_Fill.xlsx",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            return pd.read_excel(path, sheet_name="Warehouses To Fill", dtype=str).fillna("")
        except Exception:
            continue
    return pd.DataFrame()


def filter_active_warehouses(
    inventory: pd.DataFrame,
    warehouses: pd.DataFrame,
    recent_movements: pd.DataFrame,
    manual_warehouses: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if inventory.empty or "WarehouseCode" not in inventory.columns:
        return inventory, warehouses
    work = inventory.copy()
    stock_cols = [column for column in ["OnHand", "IsCommited", "OnOrder"] if column in work.columns]
    if stock_cols:
        non_empty = work[stock_cols].apply(_to_number).sum(axis=1).ne(0)
        stock_warehouses = set(work.loc[non_empty, "WarehouseCode"].astype(str).str.strip())
    else:
        stock_warehouses = set(work["WarehouseCode"].astype(str).str.strip())
    movement_warehouses = set()
    if not recent_movements.empty and "WarehouseCode" in recent_movements.columns:
        movement_warehouses = set(recent_movements["WarehouseCode"].astype(str).str.strip())
    active = stock_warehouses & movement_warehouses if movement_warehouses else stock_warehouses

    if manual_warehouses is not None and not manual_warehouses.empty and {"warehouseId", "supplyWarehouseId"}.issubset(manual_warehouses.columns):
        manual = manual_warehouses.copy()
        manual["warehouseId"] = manual["warehouseId"].astype(str).str.strip()
        manual["supplyWarehouseId"] = manual["supplyWarehouseId"].astype(str).str.strip()
        replenishable = set(manual.loc[manual["supplyWarehouseId"].ne(""), "warehouseId"])
        known_manual = set(manual["warehouseId"])
        active = {warehouse for warehouse in active if warehouse not in known_manual or warehouse in replenishable}

    filtered_inventory = work[work["WarehouseCode"].astype(str).str.strip().isin(active)].copy().reset_index(drop=True)
    if warehouses.empty or "WarehouseCode" not in warehouses.columns:
        filtered_warehouses = warehouses
    else:
        filtered_warehouses = warehouses[warehouses["WarehouseCode"].astype(str).str.strip().isin(active)].copy().reset_index(drop=True)
    return filtered_inventory, filtered_warehouses


def _read_stock_audit(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    for encoding in ["utf-8-sig", "utf-16", "cp1252", "latin-1"]:
        try:
            return pd.read_csv(path, sep="\t", dtype=str, encoding=encoding, engine="python", on_bad_lines="skip").fillna("")
        except UnicodeDecodeError:
            continue
    return pd.DataFrame()


def _part_key(value: object) -> str:
    return part_key(value)


def _sap_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%Y-%m-%d")


def _normalise_yn(value: object, default: str = "Y") -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"nan", "none", "null", "<na>"}:
        return default
    if text in {"y", "yes", "true", "1", "tyes"}:
        return "Y"
    if text in {"n", "no", "false", "0", "tno"}:
        return "N"
    return default


def _parse_any_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%Y-%m-%d")


def _parse_stock_audit_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, format="%d/%m/%y", errors="coerce")
    if pd.isna(parsed):
        parsed = pd.to_datetime(text, format="%d/%m/%Y", errors="coerce")
    if pd.isna(parsed):
        return _parse_any_date(text)
    return parsed.strftime("%Y-%m-%d")


def _clean_extracted_value(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .;")


def _parse_labeled_value(value: object, label: str) -> str:
    text = str(value or "")
    if not text.strip():
        return ""
    match = re.search(rf"{label}\s*:?\s*(.+?)(?:\r|\n|$)", text, flags=re.IGNORECASE)
    return _clean_extracted_value(match.group(1)) if match else ""


def _call_number_from_delivery_note(row: pd.Series) -> str:
    customer_ref = _clean_extracted_value(row.get("CustomerRefNumber"))
    if customer_ref:
        return customer_ref
    comments = row.get("Comments")
    return _parse_labeled_value(comments, r"Call\s*(?:Nr|No|Number)?")


def _call_match_key(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    tokens = re.findall(r"\d{6,}", text)
    if len(tokens) == 1:
        return tokens[0].lstrip("0") or "0"
    return re.sub(r"\s+", " ", text)


def _helpdesk_by_call(issue_tracker: pd.DataFrame | None) -> dict[str, dict[str, str]]:
    if issue_tracker is None or issue_tracker.empty:
        return {}
    work = issue_tracker.copy()
    for column in ["Call Number", "Created", "Status"]:
        if column not in work.columns:
            work[column] = ""
    work["CallMatchKey"] = work["Call Number"].map(_call_match_key)
    work = work[work["CallMatchKey"].astype(str).str.strip().ne("")]
    if work.empty:
        return {}
    work = work.drop_duplicates(subset=["CallMatchKey"], keep="first")
    return work.set_index("CallMatchKey")[["Created", "Status"]].to_dict("index")


def _filtered_sap_delivery_note_rows(usage: pd.DataFrame) -> pd.DataFrame:
    work = usage.copy()
    for column in ["DeliveryNoteNumber", "DocDate", "CustomerRefNumber", "Comments", "ItemNo", "WarehouseCode", "Quantity"]:
        if column not in work.columns:
            work[column] = ""
    work["QuantityNum"] = _to_number(work["Quantity"])
    return work[
        work["DocDate"].astype(str).str.strip().ne("")
        & work["ItemNo"].astype(str).str.strip().ne("")
        & work["QuantityNum"].ne(0)
    ].copy().reset_index(drop=True)


def _spi_main_alt_lookup(spi: pd.DataFrame) -> dict[str, str]:
    if spi.empty or "Main alternative par" not in spi.columns:
        return {}
    lookup: dict[str, str] = {}
    for _, row in spi.iterrows():
        main_alt = str(row.get("Main alternative par", "") or "").strip()
        if not main_alt:
            continue
        material = str(row.get("Material", "") or "").strip()
        part_number = str(row.get("PartNumber", "") or "").strip()
        for key_source in [material, part_number, _part_key(material)]:
            key = _part_key(key_source)
            if key and key not in lookup:
                lookup[key] = main_alt
    return lookup


def _spi_cocre8_cost_lookup(spi: pd.DataFrame) -> dict[str, float]:
    records = _spi_cost_records(spi)
    return {part: row["CoCre8Cost"] for part, row in _latest_cost_rows(records).items()}


def _spi_cost_records(spi: pd.DataFrame) -> pd.DataFrame:
    columns = ["PartKey", "CoCre8Cost", "SourceDate", "SourceFile"]
    if spi.empty or "ListPrice" not in spi.columns:
        return pd.DataFrame(columns=columns)
    work = spi.copy()
    for column in ["Material", "PartNumber", "SourceDate", "SourceFile"]:
        if column not in work.columns:
            work[column] = ""
    work["ListPriceNum"] = _to_number(work["ListPrice"])
    work["CoCre8Cost"] = (work["ListPriceNum"] * 0.72).round(2)
    work["SourceDateParsed"] = pd.to_datetime(work["SourceDate"], errors="coerce")
    rows: list[dict[str, object]] = []
    for _, row in work[work["CoCre8Cost"].gt(0)].iterrows():
        for column in ["PartNumber", "Material"]:
            key = _part_key(row.get(column, ""))
            if not key:
                continue
            rows.append(
                {
                    "PartKey": key,
                    "CoCre8Cost": float(row["CoCre8Cost"]),
                    "SourceDate": row["SourceDateParsed"],
                    "SourceFile": str(row.get("SourceFile", "") or "").strip(),
                }
            )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).drop_duplicates(subset=["PartKey", "CoCre8Cost", "SourceDate", "SourceFile"], keep="first")


def _latest_cost_rows(records: pd.DataFrame) -> dict[str, dict[str, object]]:
    if records.empty:
        return {}
    work = records.copy()
    work["SourceDate"] = pd.to_datetime(work["SourceDate"], errors="coerce")
    work = work.sort_values(["PartKey", "SourceDate"], ascending=[True, False], na_position="last")
    return work.drop_duplicates(subset=["PartKey"], keep="first").set_index("PartKey").to_dict("index")


def _month_end(value: object) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed + pd.offsets.MonthEnd(0)


def _cost_row_as_of(records_by_part: dict[str, pd.DataFrame], part: object, as_of: pd.Timestamp | None) -> dict[str, object] | None:
    key = _part_key(part)
    records = records_by_part.get(key)
    if records is None or records.empty:
        return None
    candidates = records
    if as_of is not None:
        dated = records[records["SourceDate"].notna() & records["SourceDate"].le(as_of)]
        if not dated.empty:
            candidates = dated
    selected = candidates.sort_values("SourceDate", ascending=False, na_position="last").iloc[0]
    return selected.to_dict()


def _cost_records_by_part(records: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if records.empty:
        return {}
    work = records.copy()
    work["SourceDate"] = pd.to_datetime(work["SourceDate"], errors="coerce")
    work = work.sort_values(["PartKey", "SourceDate"], ascending=[True, False], na_position="last")
    return {part: group.reset_index(drop=True) for part, group in work.groupby("PartKey", dropna=False)}


def _master_lookup(masters: pd.DataFrame | None) -> dict[str, str]:
    return master_lookup(masters)


def _stable_rowkey_int(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


def build_template_parts(
    parts: pd.DataFrame,
    columns: list[str],
    spi: pd.DataFrame | None = None,
    masters: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if parts.empty:
        return pd.DataFrame(columns=columns)
    spi = spi if spi is not None else pd.DataFrame()
    main_alt_by_part = _spi_main_alt_lookup(spi)
    master_by_part = _master_lookup(masters)
    out = _blank_template(columns, len(parts))
    item_keys = _col(parts, "ItemNo").map(_part_key)
    main_alt = item_keys.map(main_alt_by_part).fillna("")
    if "SPLMaster" in out.columns:
        out["SPLMaster"] = item_keys.map(master_by_part).fillna("")
    if "PartNumber" in out.columns:
        out["PartNumber"] = _col(parts, "ItemNo")
    if "primaryPartNumber" in out.columns:
        primary = main_alt.map(lambda value: _part_key(value) if str(value).strip().isdigit() else str(value).strip())
        out["primaryPartNumber"] = primary.where(primary.astype(str).str.strip().ne(""), item_keys)
    if "isPrimary" in out.columns:
        main_alt_keys = main_alt.map(_part_key)
        out["isPrimary"] = "True"
        known = main_alt_keys.astype(str).str.strip().ne("")
        out.loc[known, "isPrimary"] = (main_alt_keys[known] == item_keys[known]).map({True: "True", False: "False"})
    if "description" in out.columns:
        description = _col(parts, "ItemDescription").where(
            _col(parts, "ItemDescription").astype(str).str.strip().ne(""),
            _col(parts, "DisplayDescription"),
        )
        out["description"] = description.where(description.astype(str).str.strip().ne(""), "null")
    if "isBootStockable" in out.columns:
        out["isBootStockable"] = "N"
    if "isBranchStockable" in out.columns:
        out["isBranchStockable"] = "Y"
    if "isObsolete" in out.columns:
        out["isObsolete"] = "N"
    if "isExcludeFromReplenishment" in out.columns:
        out["isExcludeFromReplenishment"] = "N"
    if "purchaseLeadTimeDays" in out.columns:
        desc = out["description"].astype(str)
        out["purchaseLeadTimeDays"] = desc.str.contains("BBU", case=False, na=False).map({True: 180, False: 3})
    return out.drop_duplicates(subset=[col for col in ["SPLMaster", "PartNumber"] if col in out.columns], keep="first")


def build_template_parts_usage(
    usage: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if usage.empty:
        return pd.DataFrame(columns=columns)
    if {"DeliveryNoteNumber", "ItemNo", "WarehouseCode"}.issubset(usage.columns):
        return _build_template_parts_usage_from_sap_delivery_notes(usage, columns, masters, issue_tracker)
    return _build_template_parts_usage_from_stock_audit(usage, columns, masters)


def _build_template_parts_usage_from_sap_delivery_notes(
    usage: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
) -> pd.DataFrame:
    dn = _filtered_sap_delivery_note_rows(usage)
    if dn.empty:
        return pd.DataFrame(columns=columns)

    call_number = dn.apply(_call_number_from_delivery_note, axis=1)
    delivery_note = dn["DeliveryNoteNumber"].astype(str).str.strip()
    fallback_order = "DN " + delivery_note
    order_number = call_number.where(call_number.astype(str).str.strip().ne(""), fallback_order)
    comments = dn["Comments"]
    serial = comments.map(lambda value: _parse_labeled_value(value, r"Serial\s*(?:number|nr|no)?"))
    customer = comments.map(lambda value: _parse_labeled_value(value, r"Customer"))
    helpdesk = _helpdesk_by_call(issue_tracker)
    helpdesk_rows = order_number.map(lambda value: helpdesk.get(_call_match_key(value), {}))

    out = _blank_template(columns, len(dn))
    if "orderNumber" in out.columns:
        out["orderNumber"] = order_number
    if "requestId" in out.columns:
        out["requestId"] = call_number
    if "customerCompanyCode" in out.columns:
        out["customerCompanyCode"] = customer
    if "orderStartDatetime" in out.columns:
        out["orderStartDatetime"] = helpdesk_rows.map(lambda row: _parse_any_date(row.get("Created", "")))
    if "orderStatus" in out.columns:
        out["orderStatus"] = helpdesk_rows.map(lambda row: str(row.get("Status", "") or "").strip())
    if "partCode" in out.columns:
        out["partCode"] = dn["ItemNo"].map(_part_key)
    if "serialNumber" in out.columns:
        out["serialNumber"] = serial
    if "quantityUsed" in out.columns:
        out["quantityUsed"] = dn["QuantityNum"].abs()
    if "partsUsedDateTime" in out.columns:
        out["partsUsedDateTime"] = dn["DocDate"].map(_sap_date)
    if "Warehouse" in out.columns:
        out["Warehouse"] = dn["WarehouseCode"].astype(str).str.strip()
    if "deviceSerialNumber" in out.columns:
        out["deviceSerialNumber"] = serial
    if "Master" in out.columns:
        out["Master"] = dn["ItemNo"].map(_part_key).map(_master_lookup(masters)).fillna("")
    return out


def _build_template_parts_usage_from_stock_audit(
    usage: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
) -> pd.DataFrame:
    work = usage.copy()
    for column in ["Item No.", "Description"]:
        if column in work.columns:
            work[column] = work[column].replace(r"^\s*$", pd.NA, regex=True).ffill().fillna("")
    for column in ["Document", "Posting Date", "Whse", "Quantity"]:
        if column not in work.columns:
            work[column] = ""
    work["Document"] = work["Document"].astype(str).str.strip()
    work["QuantityNum"] = _to_number(work["Quantity"])
    dn = work[
        work["Document"].str.startswith("DN ", na=False)
        & work["Posting Date"].astype(str).str.strip().ne("")
        & work["Item No."].astype(str).str.strip().ne("")
        & work["QuantityNum"].lt(0)
    ].copy().reset_index(drop=True)
    if dn.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(dn))
    if "orderNumber" in out.columns:
        out["orderNumber"] = dn["Document"].astype(str).str.strip()
    if "partCode" in out.columns:
        out["partCode"] = dn["Item No."].map(_part_key)
    if "Master" in out.columns:
        out["Master"] = dn["Item No."].map(_part_key).map(_master_lookup(masters)).fillna("")
    if "Warehouse" in out.columns:
        out["Warehouse"] = dn["Whse"].astype(str).str.strip()
    if "quantityUsed" in out.columns:
        out["quantityUsed"] = dn["QuantityNum"].abs()
    if "partsUsedDateTime" in out.columns:
        parsed = pd.to_datetime(dn["Posting Date"].astype(str).str.strip(), format="%d/%m/%y", errors="coerce")
        fallback = parsed.isna()
        if fallback.any():
            parsed.loc[fallback] = pd.to_datetime(dn.loc[fallback, "Posting Date"].astype(str).str.strip(), format="%d/%m/%Y", errors="coerce")
        out["partsUsedDateTime"] = parsed.dt.strftime("%Y-%m-%d").fillna("")
    return out.drop_duplicates(keep="first")


def build_template_parts_usage_from_issue_tracker(
    issue_tracker: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if issue_tracker.empty:
        return pd.DataFrame(columns=columns)
    source = issue_tracker.copy().reset_index(drop=True)
    call_number = _col(source, "Call Number").astype(str).str.strip()
    source = source[call_number.ne("")].copy().reset_index(drop=True)
    if source.empty:
        return pd.DataFrame(columns=columns)
    part = _col(source, "DispatchPartNo").map(_part_key)
    requested_part = _col(source, "Part Nr").map(_part_key)
    part = part.where(part.astype(str).str.strip().ne(""), requested_part)
    source = source[part.astype(str).str.strip().ne("")].copy().reset_index(drop=True)
    part = part[part.astype(str).str.strip().ne("")].reset_index(drop=True)
    if source.empty:
        return pd.DataFrame(columns=columns)

    out = _blank_template(columns, len(source))
    call_number = _col(source, "Call Number").astype(str).str.strip()
    if "orderNumber" in out.columns:
        out["orderNumber"] = call_number
    if "requestId" in out.columns:
        out["requestId"] = call_number
    if "customerCompanyCode" in out.columns:
        customer = _col(source, "CustomerNormalized").astype(str).str.strip()
        fallback = _col(source, "Customer").astype(str).str.strip()
        out["customerCompanyCode"] = customer.where(customer.ne(""), fallback)
    if "orderStartDatetime" in out.columns:
        out["orderStartDatetime"] = _col(source, "Created").map(_parse_any_date)
    if "orderStatus" in out.columns:
        out["orderStatus"] = _col(source, "Status")
    if "partCode" in out.columns:
        out["partCode"] = part
    if "serialNumber" in out.columns:
        out["serialNumber"] = _col(source, "Serial Nr")
    if "quantityUsed" in out.columns:
        out["quantityUsed"] = _to_number(_col(source, "Quantity"))
    if "partsUsedDateTime" in out.columns:
        out["partsUsedDateTime"] = _col(source, "Created").map(_parse_any_date)
    if "Warehouse" in out.columns:
        out["Warehouse"] = _col(source, "DispatchWarehouse")
    if "deviceSerialNumber" in out.columns:
        out["deviceSerialNumber"] = _col(source, "Serial Nr")
    if "Master" in out.columns:
        by_part = _master_lookup(masters)
        mapped = part.map(by_part).fillna("")
        tracker_master = _col(source, "SPLMaster").astype(str).str.strip()
        out["Master"] = tracker_master.where(tracker_master.ne(""), mapped)
    return out.drop_duplicates(keep="first")


def build_template_purchase_orders(
    purchase_orders: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
    spi: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if purchase_orders.empty:
        return pd.DataFrame(columns=columns)
    source = purchase_orders.copy().reset_index(drop=True)
    out = _blank_template(columns, len(source))
    if "purchaseOrderNumber" in out.columns:
        out["purchaseOrderNumber"] = _col(source, "PurchaseOrderNumber").astype(str).str.strip()
    if "purchaseOrderStatus" in out.columns:
        canceled = _col(source, "Canceled").astype(str).str.upper().eq("Y")
        doc_status = _col(source, "DocStatus").astype(str).str.upper()
        out["purchaseOrderStatus"] = "Accepted"
        out.loc[doc_status.eq("C"), "purchaseOrderStatus"] = "Fulfilled"
        out.loc[canceled, "purchaseOrderStatus"] = "Cancelled"
    if "creationDateTime" in out.columns:
        out["creationDateTime"] = _col(source, "CreationDateTime").map(_sap_date)
    if "approvalDateTime" in out.columns:
        out["approvalDateTime"] = _col(source, "ApprovalDateTime").map(_sap_date)
    if "toWarehouseId" in out.columns:
        out["toWarehouseId"] = _col(source, "ToWarehouseId")
    if "vendorId" in out.columns:
        out["vendorId"] = _col(source, "VendorId")
    if "partNumber" in out.columns:
        out["partNumber"] = _col(source, "PartNumber").map(_part_key)
    if "quantity" in out.columns:
        out["quantity"] = _to_number(_col(source, "Quantity"))
    if "lineCost" in out.columns:
        records_by_part = _cost_records_by_part(_spi_cost_records(spi if spi is not None else pd.DataFrame()))
        po_dates = _col(source, "ApprovalDateTime").map(_month_end)
        out["lineCost"] = [
            (row.get("CoCre8Cost") if row else "")
            for row in (
                _cost_row_as_of(records_by_part, part, as_of)
                for part, as_of in zip(_col(source, "PartNumber"), po_dates)
            )
        ]
    if "quantityReceived" in out.columns:
        out["quantityReceived"] = _to_number(_col(source, "QuantityReceived"))
    if "receivedDateTime" in out.columns:
        out["receivedDateTime"] = _col(source, "ReceivedDateTime").map(_sap_date)
    if "demandStatus" in out.columns and issue_tracker is not None and not issue_tracker.empty:
        matches = purchase_order_ticket_matches(source, issue_tracker, masters)
        if not matches.empty:
            status_by_key = (
                matches[matches["ReplenishStatus"].astype(str).str.strip().ne("")]
                .drop_duplicates(subset=["PurchaseOrderNumber", "SapPartNumber"], keep="first")
                .set_index(["PurchaseOrderNumber", "SapPartNumber"])["ReplenishStatus"]
                .to_dict()
            )
            keys = list(zip(out.get("purchaseOrderNumber", pd.Series([""] * len(out))).map(_part_key), out.get("partNumber", pd.Series([""] * len(out))).map(_part_key)))
            out["demandStatus"] = [status_by_key.get(key, "") for key in keys]
    return out.drop_duplicates(keep="first")


def _part_cost_rows(
    parts: pd.DataFrame,
    purchase_orders: pd.DataFrame,
    spi: pd.DataFrame,
    masters: pd.DataFrame | None = None,
) -> pd.DataFrame:
    columns = [
        "partCode",
        "SPLMaster",
        "cost",
        "currencyCode",
        "averageCost",
        "averageRepairCost",
        "lastPoDate",
        "costSourceDate",
        "costSourceFile",
        "poWeightedQuantity",
        "poWeightedRows",
        "averageCostBasis",
        "EvidenceStatus",
    ]
    records = _spi_cost_records(spi)
    if records.empty:
        return pd.DataFrame(columns=columns)
    records_by_part = _cost_records_by_part(records)
    by_part = _master_lookup(masters)

    source_parts: set[str] = set()
    if not parts.empty:
        item_keys = _col(parts, "ItemNo").map(_part_key)
        if "SPLMaster" in parts.columns:
            linked = parts["SPLMaster"].astype(str).str.strip().ne("")
            source_parts.update(item_keys[linked].dropna().astype(str).str.strip())
        else:
            source_parts.update(item_keys.dropna().astype(str).str.strip())
    if purchase_orders is not None and not purchase_orders.empty:
        source_parts.update(_col(purchase_orders, "PartNumber").map(_part_key).dropna().astype(str).str.strip())
    source_parts.discard("")

    po = purchase_orders.copy() if purchase_orders is not None and not purchase_orders.empty else pd.DataFrame()
    if po.empty:
        po = pd.DataFrame(columns=["PartNumber", "ApprovalDateTime", "Quantity"])
    po["PartKey"] = _col(po, "PartNumber").map(_part_key)
    po["PoMonthEnd"] = _col(po, "ApprovalDateTime").map(_month_end)
    po["PoDate"] = _col(po, "ApprovalDateTime").map(_sap_date)
    po["QuantityNum"] = _to_number(_col(po, "Quantity"))

    rows: list[dict[str, object]] = []
    for part in sorted(source_parts):
        part_pos = po[po["PartKey"].eq(part)].copy()
        last_po_month = None
        last_po_date = ""
        if not part_pos.empty:
            dated = part_pos[part_pos["PoMonthEnd"].notna()].sort_values("PoMonthEnd")
            if not dated.empty:
                last_po_month = dated.iloc[-1]["PoMonthEnd"]
                last_po_date = str(dated.iloc[-1]["PoDate"] or "")
        cost_row = _cost_row_as_of(records_by_part, part, last_po_month)
        if not cost_row:
            continue

        weighted_total = 0.0
        weighted_qty = 0.0
        weighted_rows = 0
        for _, po_row in part_pos.iterrows():
            qty = float(po_row.get("QuantityNum", 0) or 0)
            if qty <= 0:
                continue
            po_cost = _cost_row_as_of(records_by_part, part, po_row.get("PoMonthEnd"))
            if not po_cost:
                continue
            weighted_total += float(po_cost["CoCre8Cost"]) * qty
            weighted_qty += qty
            weighted_rows += 1
        if weighted_qty > 0:
            average_cost = round(weighted_total / weighted_qty, 2)
            average_basis = "PO quantity weighted"
        else:
            average_cost = float(cost_row["CoCre8Cost"])
            average_basis = "No PO quantity; averageCost set to latest available cost"

        source_date = cost_row.get("SourceDate")
        source_date_text = "" if pd.isna(source_date) else pd.Timestamp(source_date).date().isoformat()
        rows.append(
            {
                "partCode": part,
                "SPLMaster": by_part.get(part, ""),
                "cost": float(cost_row["CoCre8Cost"]),
                "currencyCode": "EUR",
                "averageCost": average_cost,
                "averageRepairCost": "",
                "lastPoDate": last_po_date,
                "costSourceDate": source_date_text,
                "costSourceFile": str(cost_row.get("SourceFile", "") or ""),
                "poWeightedQuantity": round(weighted_qty, 2) if weighted_qty else "",
                "poWeightedRows": weighted_rows if weighted_rows else "",
                "averageCostBasis": average_basis,
                "EvidenceStatus": "Ready" if by_part.get(part, "") else "Ready; no SPL Master mapping",
            }
        )
    return pd.DataFrame(rows, columns=columns)


def build_template_part_cost(
    parts: pd.DataFrame,
    purchase_orders: pd.DataFrame,
    columns: list[str],
    spi: pd.DataFrame,
    masters: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows = _part_cost_rows(parts, purchase_orders, spi, masters)
    if rows.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(rows))
    for column in ["partCode", "cost", "currencyCode", "averageCost", "averageRepairCost"]:
        if column in out.columns:
            out[column] = rows[column]
    return out.drop_duplicates(subset=["partCode"], keep="first") if "partCode" in out.columns else out


def build_parts_usage_evidence(usage: pd.DataFrame, masters: pd.DataFrame | None) -> pd.DataFrame:
    columns = [
        "Source",
        "deliveryNoteNumber",
        "orderNumber",
        "requestId",
        "customerCompanyCode",
        "actualPartNumber",
        "serialNumber",
        "SPLMaster",
        "Warehouse",
        "quantityUsed",
        "partsUsedDateTime",
        "EvidenceStatus",
    ]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    source_name = "SAP Delivery Notes ODLN/DLN1" if "DeliveryNoteNumber" in usage.columns else "Stock Audit Report 3Y"
    template = build_template_parts_usage(
        usage,
        [
            "orderNumber",
            "requestId",
            "customerCompanyCode",
            "partCode",
            "serialNumber",
            "Warehouse",
            "quantityUsed",
            "partsUsedDateTime",
        ],
        masters,
    )
    if template.empty:
        return pd.DataFrame(columns=columns)
    by_part = _master_lookup(masters)
    if "DeliveryNoteNumber" in usage.columns:
        source_rows = _filtered_sap_delivery_note_rows(usage)
        delivery_notes = _col(source_rows, "DeliveryNoteNumber").astype(str).str.strip()
    else:
        delivery_notes = pd.Series([""] * len(template))
    delivery_notes = delivery_notes.reset_index(drop=True).reindex(range(len(template)), fill_value="")
    out = pd.DataFrame(
        {
            "Source": source_name,
            "deliveryNoteNumber": delivery_notes,
            "orderNumber": template["orderNumber"],
            "requestId": template.get("requestId", pd.Series([""] * len(template))),
            "customerCompanyCode": template.get("customerCompanyCode", pd.Series([""] * len(template))),
            "actualPartNumber": template["partCode"],
            "serialNumber": template.get("serialNumber", pd.Series([""] * len(template))),
            "SPLMaster": template["partCode"].map(by_part).fillna(""),
            "Warehouse": template["Warehouse"],
            "quantityUsed": template["quantityUsed"],
            "partsUsedDateTime": template["partsUsedDateTime"],
        }
    )
    out["EvidenceStatus"] = "Mapped to SPL Master"
    out.loc[out["SPLMaster"].astype(str).str.strip().eq(""), "EvidenceStatus"] = "Needs SPL Master mapping"
    return out


def build_purchase_order_evidence(
    purchase_orders: pd.DataFrame,
    masters: pd.DataFrame | None,
    issue_tracker: pd.DataFrame | None,
) -> pd.DataFrame:
    columns = [
        "PurchaseOrderNumber",
        "SapInternalPurchaseOrderNumber",
        "actualPartNumber",
        "SPLMaster",
        "quantity",
        "quantityReceived",
        "toWarehouseId",
        "vendorId",
        "TicketCallNumber",
        "TicketMSConvoID",
        "TicketPartNumber",
        "TicketSPLMaster",
        "TicketReplenishStatus",
        "MatchType",
        "EvidenceStatus",
    ]
    if purchase_orders.empty:
        return pd.DataFrame(columns=columns)
    by_part = _master_lookup(masters)
    source = purchase_orders.copy().reset_index(drop=True)
    out = pd.DataFrame(
        {
            "PurchaseOrderNumber": _col(source, "PurchaseOrderNumber").map(_part_key),
            "SapInternalPurchaseOrderNumber": _col(source, "SapInternalPurchaseOrderNumber").map(_part_key),
            "actualPartNumber": _col(source, "PartNumber").map(_part_key),
            "SPLMaster": _col(source, "PartNumber").map(_part_key).map(by_part).fillna(""),
            "quantity": _to_number(_col(source, "Quantity")),
            "quantityReceived": _to_number(_col(source, "QuantityReceived")),
            "toWarehouseId": _col(source, "ToWarehouseId"),
            "vendorId": _col(source, "VendorId"),
        }
    )
    out["TicketCallNumber"] = ""
    out["TicketMSConvoID"] = ""
    out["TicketPartNumber"] = ""
    out["TicketSPLMaster"] = ""
    out["TicketReplenishStatus"] = ""
    out["MatchType"] = ""
    out["EvidenceStatus"] = "SAP PO line only"
    out.loc[out["SPLMaster"].astype(str).str.strip().eq(""), "EvidenceStatus"] = "SAP PO line only; needs SPL Master mapping"

    if issue_tracker is not None and not issue_tracker.empty:
        matches = purchase_order_ticket_matches(source, issue_tracker, masters)
        if not matches.empty:
            first_match = matches.drop_duplicates(subset=["PurchaseOrderNumber", "SapPartNumber"], keep="first")
            match_lookup = first_match.set_index(["PurchaseOrderNumber", "SapPartNumber"]).to_dict("index")
            for idx, row in out.iterrows():
                match = match_lookup.get((row["PurchaseOrderNumber"], row["actualPartNumber"]))
                if not match:
                    continue
                out.at[idx, "TicketCallNumber"] = match["CallNumber"]
                out.at[idx, "TicketMSConvoID"] = match["MSConvoID"]
                out.at[idx, "TicketPartNumber"] = match["TicketPartNumber"]
                out.at[idx, "TicketSPLMaster"] = match["TicketSPLMaster"]
                out.at[idx, "TicketReplenishStatus"] = match["ReplenishStatus"]
                out.at[idx, "MatchType"] = match["MatchType"]
                out.at[idx, "EvidenceStatus"] = "Matched to issue tracker"
    return out


def write_review_evidence(
    evidence_dir: Path,
    usage: pd.DataFrame,
    purchase_orders: pd.DataFrame,
    issue_tracker: pd.DataFrame,
    masters: pd.DataFrame | None,
    manual_warehouses: pd.DataFrame | None = None,
    parts: pd.DataFrame | None = None,
    spi_costs: pd.DataFrame | None = None,
) -> list[Path]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    files = [
        (build_parts_usage_evidence(usage, masters), evidence_dir / "PartsUsage_SPLMaster_Evidence.csv"),
        (build_purchase_order_evidence(purchase_orders, masters, issue_tracker), evidence_dir / "PurchaseOrders_SPLMaster_Evidence.csv"),
        (purchase_order_reconciliation(purchase_orders, issue_tracker, masters), evidence_dir / "PurchaseOrders_Ticket_Reconciliation.csv"),
        (issue_tracker_evidence_rows(issue_tracker, masters), evidence_dir / "IssueTracker_Line_Evidence.csv"),
        (
            _part_cost_rows(
                parts if parts is not None else pd.DataFrame(),
                purchase_orders,
                spi_costs if spi_costs is not None else pd.DataFrame(),
                masters,
            ),
            evidence_dir / "PartCost_Evidence.csv",
        ),
    ]
    if manual_warehouses is not None and not manual_warehouses.empty:
        files.append((build_warehouse_manual_evidence(manual_warehouses), evidence_dir / "Warehouses_Manual_Evidence.csv"))
    return [_write_csv(df, path) for df, path in files]


def build_template_warehouses(
    warehouses: pd.DataFrame,
    columns: list[str],
    manual_warehouses: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if warehouses.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(warehouses))
    if "warehouseId" in out.columns:
        out["warehouseId"] = _col(warehouses, "WarehouseCode")
    if "warehouseDescription" in out.columns:
        out["warehouseDescription"] = _col(warehouses, "WarehouseName")
    if manual_warehouses is not None and not manual_warehouses.empty and "warehouseId" in manual_warehouses.columns:
        manual = manual_warehouses.copy()
        manual["warehouseId"] = manual["warehouseId"].astype(str).str.strip()
        manual_by_id = manual.drop_duplicates(subset=["warehouseId"], keep="first").set_index("warehouseId")
        warehouse_ids = out["warehouseId"].astype(str).str.strip() if "warehouseId" in out.columns else pd.Series([""] * len(out))
        for column in [
            "addressId",
            "returnWarehouseId",
            "supplyWarehouseId",
            "warehouseTypeId",
            "isReplenishable",
            "isBranchStockable",
            "isRemote",
            "warehouseStatusId",
        ]:
            if column in out.columns and column in manual_by_id.columns:
                out[column] = warehouse_ids.map(manual_by_id[column]).fillna("")
        if "warehouseStatusId" in out.columns and "isObsolete" in manual_by_id.columns:
            obsolete = warehouse_ids.map(manual_by_id["isObsolete"]).fillna("").astype(str).str.strip().str.upper()
            out["warehouseStatusId"] = ""
            known = obsolete.isin(["Y", "N"])
            out.loc[known, "warehouseStatusId"] = obsolete.loc[known].map({"Y": "N", "N": "Y"})
    return out.drop_duplicates(subset=["warehouseId"], keep="first") if "warehouseId" in out.columns else out


def build_warehouse_manual_evidence(manual_warehouses: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "warehouseId",
        "warehouseDescription",
        "addressId",
        "returnWarehouseId",
        "supplyWarehouseId",
        "warehouseTypeId",
        "isReplenishable",
        "isBranchStockable",
        "isRemote",
        "warehouseStatusId",
        "isObsolete",
        "activityEvidence",
        "Notes",
    ]
    out = manual_warehouses.copy()
    for column in columns:
        if column not in out.columns:
            out[column] = ""
    return out[columns].drop_duplicates(subset=["warehouseId"], keep="first")


def build_template_stock_on_hand(
    inventory: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if inventory.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(inventory))
    if "partCode" in out.columns:
        out["partCode"] = _col(inventory, "ItemNo")
    if "partNumber" in out.columns:
        out["partNumber"] = _col(inventory, "ItemNo")
    if "warehouseCode" in out.columns:
        out["warehouseCode"] = _col(inventory, "WarehouseCode")
    if "quantityAllocated" in out.columns:
        out["quantityAllocated"] = _to_number(_first_col(inventory, ["IsCommited", "Commited"]))
    if "quantityOnHand" in out.columns:
        out["quantityOnHand"] = _to_number(_first_col(inventory, ["OnHand", "Quantity"]))
    if "quantityInbound" in out.columns:
        out["quantityInbound"] = _to_number(_first_col(inventory, ["OnOrder", "Ordered"]))
    if "uniqueId" in out.columns:
        by_part = _master_lookup(masters)
        part_keys = _col(inventory, "ItemNo").map(_part_key)
        master_keys = part_keys.map(by_part).fillna("")
        warehouse_keys = _col(inventory, "WarehouseCode").astype(str).str.strip()
        row_keys = part_keys.astype(str) + "|" + master_keys.astype(str) + "|" + warehouse_keys.astype(str)
        out["uniqueId"] = row_keys.map(_stable_rowkey_int)
    subset = [col for col in ["partCode", "partNumber", "warehouseCode"] if col in out.columns]
    return out.drop_duplicates(subset=subset, keep="first") if subset else out


def build_template_vendors(purchase_orders: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if purchase_orders.empty:
        return pd.DataFrame(columns=columns)
    source = purchase_orders[_col(purchase_orders, "VendorId").astype(str).str.strip().ne("")].copy()
    if source.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(source))
    if "vendorId" in out.columns:
        out["vendorId"] = _col(source, "VendorId")
    if "Description" in out.columns:
        out["Description"] = _first_col(source, ["VendorName", "Description", "CardName"])
    if "isActive" in out.columns:
        out["isActive"] = _first_col(source, ["VendorIsActive", "isActive", "ValidFor"], default="Y").map(_normalise_yn)
    if "vendorId" in out.columns:
        return out.drop_duplicates(subset=["vendorId"], keep="first").reset_index(drop=True)
    return out


def build_template_inventory_transfers(stock_audit: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if stock_audit.empty:
        return pd.DataFrame(columns=columns)

    source = stock_audit.copy()
    source.columns = [str(column).strip() for column in source.columns]
    required = {"Item No.", "Posting Date", "Document", "Whse", "Quantity"}
    if not required.issubset(source.columns):
        return pd.DataFrame(columns=columns)

    for column in ["Item No.", "Description"]:
        if column in source.columns:
            source[column] = source[column].replace(r"^\s*$", pd.NA, regex=True).ffill().fillna("")

    transfers = source[source["Document"].astype(str).str.startswith("IM ", na=False)].copy()
    if transfers.empty:
        return pd.DataFrame(columns=columns)
    transfers["QuantityNum"] = _to_number(transfers["Quantity"])
    transfers["AbsQty"] = transfers["QuantityNum"].abs()

    rows: list[dict[str, object]] = []
    group_columns = ["Document", "Item No.", "AbsQty"]
    for (document, item_no, abs_qty), group in transfers.groupby(group_columns, dropna=False, sort=False):
        negative = group[group["QuantityNum"] < 0].copy()
        positive = group[group["QuantityNum"] > 0].copy()
        if negative.empty or positive.empty or len(negative) != len(positive):
            continue

        negative = negative.sort_index().reset_index(drop=True)
        positive = positive.sort_index().reset_index(drop=True)
        for pair_index in range(len(negative)):
            from_row = negative.iloc[pair_index]
            to_row = positive.iloc[pair_index]
            transfer_id = f"{document}|{item_no}|{abs_qty:g}"
            if len(negative) > 1:
                transfer_id = f"{transfer_id}|{pair_index + 1}"
            posting_date = _parse_stock_audit_date(from_row.get("Posting Date") or to_row.get("Posting Date"))
            rows.append(
                {
                    "inventoryTransferImoId": transfer_id,
                    "createdDateTime": posting_date,
                    "completedDateTime": posting_date,
                    "fromWarehouseId": str(from_row.get("Whse", "")).strip(),
                    "toWarehouseId": str(to_row.get("Whse", "")).strip(),
                    "demandStatus": "Fulfilled",
                    "orderStatusIsClosed": "Y",
                    "movementType": "",
                    "addressId": "",
                    "isResolved": "Y",
                    "partNumber": str(item_no).strip(),
                    "quantity": abs_qty,
                    "shipListCode": "",
                }
            )

    if not rows:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(rows)
    for column in columns:
        if column not in out.columns:
            out[column] = ""
    return out[columns].drop_duplicates(subset=["inventoryTransferImoId"], keep="first").reset_index(drop=True)


def build_template_customers(customers: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if customers.empty:
        return pd.DataFrame(columns=columns)
    source = customers[_col(customers, "CustomerCode").astype(str).str.strip().ne("")].copy()
    if source.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(source))
    if "customerId" in out.columns:
        out["customerId"] = _col(source, "CustomerCode")
    if "Description" in out.columns:
        out["Description"] = _col(source, "CustomerName")
    if "customerName" in out.columns:
        out["customerName"] = _col(source, "CustomerName")
    return out.drop_duplicates(subset=["customerId"], keep="first") if "customerId" in out.columns else out


def validate_template_outputs(
    csv_dir: Path,
    outputs: dict[str, pd.DataFrame],
    templates: dict[str, list[str]],
) -> pd.DataFrame:
    rows = []
    for object_name, columns in templates.items():
        df = outputs.get(object_name, pd.DataFrame(columns=columns))
        scoped_columns = [
            column
            for column in columns
            if (object_name, column) not in OUT_OF_SCOPE_TEMPLATE_FIELDS
        ]
        populated_fields = [
            column
            for column in scoped_columns
            if column in df.columns and len(df) > 0 and df[column].astype(str).str.strip().ne("").any()
        ]
        missing = [column for column in scoped_columns if column not in df.columns]
        if missing:
            status = "FAIL"
            notes = f"Missing template columns: {', '.join(missing)}"
        elif not scoped_columns:
            status = "OUT_OF_SCOPE"
            notes = "All template fields are out of CoCre8 v1 scope."
        elif len(df) == 0:
            status = "PENDING"
            notes = "Header-only template; no confirmed CoCre8 source yet."
        else:
            row_required_blanks = []
            for column in ROW_REQUIRED_TEMPLATE_FIELDS.get(object_name, []):
                if column in df.columns:
                    blank_count = int(df[column].astype(str).str.strip().eq("").sum())
                    if blank_count:
                        row_required_blanks.append(f"{column} blank on {blank_count} rows")
            if len(populated_fields) == len(scoped_columns) and not row_required_blanks:
                status = "PASS"
                notes = "All in-scope template fields populated."
                rows.append(
                    {
                        "Object": object_name,
                        "Path": str(csv_dir / f"{object_name}.csv"),
                        "Rows": len(df),
                        "Status": status,
                        "Notes": notes,
                    }
                )
                continue
            status = "PARTIAL"
            blank_fields = [column for column in scoped_columns if column not in populated_fields]
            notes = "Populated: " + ", ".join(populated_fields)
            if blank_fields:
                notes += " | Needs source confirmation: " + ", ".join(blank_fields)
            if row_required_blanks:
                notes += " | Row-level gaps: " + "; ".join(row_required_blanks)
        rows.append(
            {
                "Object": object_name,
                "Path": str(csv_dir / f"{object_name}.csv"),
                "Rows": len(df),
                "Status": status,
                "Notes": notes,
            }
        )
    return pd.DataFrame(rows)


def build_spl_masters(parts: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["spl_masters"]
    if parts.empty:
        return pd.DataFrame(columns=columns)
    work = parts.copy()
    work["Primary Part"] = _col(work, "DisplayItemNo")
    work["Description"] = _col(work, "DisplayDescription").where(_col(work, "DisplayDescription").astype(str).str.strip().ne(""), _col(work, "ItemDescription"))
    work["SPL Boot Stockable"] = ""
    work["Boot Stockable"] = ""
    work["Branch Stockable"] = ""
    work["Product Class ID"] = ""
    work["Product Type ID"] = ""
    out = pd.DataFrame(
        {
            "part_id": _col(work, "ItemNo"),
            "SPL MASTER": _col(work, "SPLMaster"),
            "Primary Part": work["Primary Part"],
            "Description": work["Description"],
            "SPL Boot Stockable": work["SPL Boot Stockable"],
            "Boot Stockable": work["Boot Stockable"],
            "Branch Stockable": work["Branch Stockable"],
            "Product Class ID": work["Product Class ID"],
            "Product Type ID": work["Product Type ID"],
        }
    )
    return out.drop_duplicates(subset=["part_id"], keep="first")


def build_part_alternatives(parts: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["part_alternatives"]
    if parts.empty:
        return pd.DataFrame(columns=columns)
    work = parts.copy()
    grouped = work[_col(work, "SPLMaster").astype(str).str.strip().ne("")]
    rows = []
    for _, group in grouped.groupby("SPLMaster", dropna=False):
        members = _col(group, "ItemNo").astype(str).str.strip().dropna().tolist()
        for part in members:
            for alt in members:
                if part and alt and part != alt:
                    rows.append({"part_id": part, "alt_part_id": alt})
    return pd.DataFrame(rows, columns=columns).drop_duplicates() if rows else pd.DataFrame(columns=columns)


def build_parts(parts: pd.DataFrame, inventory: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["parts"]
    source = _first_existing(parts, inventory)
    if source.empty:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(
        {
            "part_id": _col(source, "ItemNo").where(_col(source, "ItemNo").astype(str).str.strip().ne(""), _col(source, "Item No.")),
            "description": _col(source, "ItemDescription").where(_col(source, "ItemDescription").astype(str).str.strip().ne(""), _col(source, "DisplayDescription")),
            "spl_master": _col(source, "SPLMaster"),
            "primary_part": _col(source, "DisplayItemNo").where(_col(source, "DisplayItemNo").astype(str).str.strip().ne(""), _col(source, "ItemNo")),
            "return_code": _col(source, "ReturnCode"),
            "repair_code": _col(source, "RepairCode"),
            "list_price": _col(source, "ListPrice"),
            "credit_price": _col(source, "CreditPrice"),
            "cocre8_unit_cost": _col(source, "CoCre8UnitCost").where(_col(source, "CoCre8UnitCost").astype(str).str.strip().ne(""), _col(source, "CoCre8Cost")),
            "currency_id": _col(source, "Currency"),
        }
    )
    return out.drop_duplicates(subset=["part_id"], keep="first")


def build_warehouses(warehouses: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["warehouses"]
    if warehouses.empty:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(
        {
            "warehouse_id": _col(warehouses, "WarehouseCode"),
            "descr": _col(warehouses, "WarehouseName"),
            "location": _col(warehouses, "Location"),
            "warehouse_type_id": _col(warehouses, "WarehouseType"),
            "customer_code": _col(warehouses, "CustomerCode"),
            "customer_name": _col(warehouses, "CustomerName"),
        }
    )
    return out.drop_duplicates(subset=["warehouse_id"], keep="first")


def build_customers(customers: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["customers"]
    if customers.empty:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(
        {
            "company_id": _col(customers, "CustomerCode"),
            "customer_name": _col(customers, "CustomerName"),
            "warehouse_id": _col(customers, "WarehouseCode"),
            "location": _col(customers, "Location"),
        }
    )
    return out.drop_duplicates()


def build_stock_detail(inventory: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["stock_detail"]
    if inventory.empty:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(
        {
            "part_id": _col(inventory, "ItemNo"),
            "warehouse_id": _col(inventory, "WarehouseCode"),
            "qty": _col(inventory, "Quantity"),
            "allocated_qty": _col(inventory, "Commited"),
            "in_bound_qty": _col(inventory, "Ordered"),
            "out_bound_qty": "",
            "minimum_stock": _col(inventory, "Minimum Stock"),
            "maximum_stock": _col(inventory, "Maximum Stock"),
            "inv_type_id": "good",
        }
    )
    for col in ["qty", "allocated_qty", "in_bound_qty", "out_bound_qty", "minimum_stock", "maximum_stock"]:
        out[col] = _to_number(out[col])
    return out.drop_duplicates(subset=["part_id", "warehouse_id"], keep="first")


def build_usage(usage: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["usage"]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(
        {
            "posting_date": _col(usage, "PostingDate"),
            "order_id": _col(usage, "Document"),
            "part_id": _col(usage, "ItemNo"),
            "warehouse_id": _col(usage, "WarehouseCode"),
            "qty_used": _col(usage, "QuantityAbs"),
            "customer_id": _col(usage, "CustomerCode"),
            "device_serial": "",
        }
    )
    out["qty_used"] = _to_number(out["qty_used"])
    return out


def build_stock_flow(stock_flow: pd.DataFrame) -> pd.DataFrame:
    columns = CONFIRMED_OUTPUT_OBJECTS["stock_flow"]
    if stock_flow.empty:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(
        {
            "posting_date": _col(stock_flow, "PostingDate"),
            "document": _col(stock_flow, "Document"),
            "document_type": _col(stock_flow, "DocumentType"),
            "part_id": _col(stock_flow, "ItemNo"),
            "warehouse_id": _col(stock_flow, "WarehouseCode"),
            "quantity_signed": _col(stock_flow, "QuantitySigned"),
        }
    )
    out["quantity_signed"] = _to_number(out["quantity_signed"])
    return out


def validate_outputs(csv_dir: Path, template_dir: Path) -> pd.DataFrame:
    rows = []
    for object_name, columns in CONFIRMED_OUTPUT_OBJECTS.items():
        path = csv_dir / f"{object_name}.csv"
        df = _read_csv(path)
        missing = [column for column in columns if column not in df.columns]
        rows.append(
            {
                "Object": object_name,
                "Path": str(path),
                "Rows": len(df),
                "Status": "PASS" if not missing else "FAIL",
                "Notes": "" if not missing else f"Missing columns: {', '.join(missing)}",
            }
        )
    for object_name, columns in PENDING_OUTPUT_OBJECTS.items():
        path = template_dir / f"{object_name}.csv"
        df = _read_csv(path)
        rows.append(
            {
                "Object": object_name,
                "Path": str(path),
                "Rows": len(df),
                "Status": "PENDING",
                "Notes": f"Header-only investigation template with {len(columns)} fields.",
            }
        )
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate CoCre8 Planning V2 onboarding CSVs.")
    parser.add_argument("--out", type=Path, default=Path("data") / "output" / "onboarding_csvs")
    args = parser.parse_args(argv)

    cfg = get_config()
    written = generate_onboarding_csvs(cfg, args.out)
    print(f"Wrote {len(written)} onboarding files under {args.out.parent}")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
