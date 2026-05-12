"""Generate confirmed Planning V2 onboarding CSVs from live CoCre8 SAP sources."""

from __future__ import annotations

import argparse
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
from planning_v2.sap_extracts import fetch_live_purchase_orders, fetch_live_template_sources
from planning_v2.template_specs import template_columns


POPULATED_TEMPLATE_FIELDS = {
    "Parts": {
        "SPLMaster": "Reference masters.csv:SPL Master by linked item",
        "PartNumber": "SAP Service Layer SQLQueries:OITM.ItemCode",
        "isPrimary": "SPI_DATA.csv:Main alternative par equals material/part number",
        "primaryPartNumber": "SPI_DATA.csv:Main alternative par",
        "description": "SAP Service Layer SQLQueries:OITM.ItemName",
    },
    "Warehouses": {
        "warehouseId": "SAP Service Layer Warehouses:WarehouseCode",
        "warehouseDescription": "SAP Service Layer Warehouses:WarehouseName",
    },
    "WarehouseStockOnHand": {
        "partCode": "SAP Service Layer SQLQueries:OITW.ItemCode",
        "warehouseCode": "SAP Service Layer SQLQueries:OITW.WhsCode",
        "quantityAllocated": "SAP Service Layer SQLQueries:OITW.IsCommited",
        "quantityOnHand": "SAP Service Layer SQLQueries:OITW.OnHand",
        "quantityInbound": "SAP Service Layer SQLQueries:OITW.OnOrder",
    },
    "Customers": {},
    "PartsUsage": {
        "orderNumber": "Stock Audit Report 3Y:Document for DN rows",
        "partCode": "Stock Audit Report 3Y:Item No.",
        "Warehouse": "Stock Audit Report 3Y:Whse",
        "quantityUsed": "Stock Audit Report 3Y:absolute Quantity for negative DN rows",
        "partsUsedDateTime": "Stock Audit Report 3Y:Posting Date",
    },
    "PurchaseOrders": {
        "purchaseOrderNumber": "SAP Service Layer SQLQueries:OPOR.DocNum",
        "purchaseOrderStatus": "SAP Service Layer SQLQueries:OPOR.DocStatus/CANCELED mapped to template status",
        "creationDateTime": "SAP Service Layer SQLQueries:OPOR.CreateDate",
        "approvalDateTime": "SAP Service Layer SQLQueries:OPOR.DocDate",
        "toWarehouseId": "SAP Service Layer SQLQueries:POR1.WhsCode",
        "vendorId": "SAP Service Layer SQLQueries:OPOR.CardCode",
        "partNumber": "SAP Service Layer SQLQueries:POR1.ItemCode",
        "quantity": "SAP Service Layer SQLQueries:POR1.Quantity",
        "lineCost": "SAP Service Layer SQLQueries:POR1.LineTotal",
        "quantityReceived": "SAP Service Layer SQLQueries:PDN1.Quantity summed by PO line",
        "receivedDateTime": "SAP Service Layer SQLQueries:OPDN.DocDate max by PO line",
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
    ("Parts", "isService"),
    ("Parts", "isTool"),
    ("Parts", "isSmallPart"),
    ("PartsUsage", "requestId"),
    ("PurchaseOrders", "customerId"),
    ("PurchaseOrders", "requestTicketDateTime"),
    ("PurchaseOrders", "isResolved"),
    ("Warehouses", "nodeId"),
    ("Warehouses", "isRepairWarehouse"),
    ("Warehouses", "isBootStockable"),
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
    usage = _read_stock_audit(_stock_audit_3y_path(cfg))
    stock_flow = pd.DataFrame()
    customers = pd.DataFrame()
    masters = _read_masters(cfg.reference_dir / "masters.csv")
    spi = _read_spi(cfg.exco_source_dir / "SPI_DATA.csv")
    issue_tracker = read_issue_tracker(cfg.issue_tracker_csv)

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
    )

    for object_name, df in outputs.items():
        written.append(_write_csv(df, csv_dir / f"{object_name}.csv"))

    validation = validate_template_outputs(csv_dir, outputs, templates)
    written.append(_write_csv(validation, out_dir.parent / "validation_summary.csv"))
    written.extend(write_review_evidence(out_dir.parent / "review_evidence", usage, purchase_orders, issue_tracker, masters))
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
) -> dict[str, pd.DataFrame]:
    outputs: dict[str, pd.DataFrame] = {}
    for object_name, columns in templates.items():
        if object_name == "Parts":
            outputs[object_name] = build_template_parts(parts, columns, spi, masters)
        elif object_name == "Warehouses":
            outputs[object_name] = build_template_warehouses(warehouses, columns)
        elif object_name == "WarehouseStockOnHand":
            outputs[object_name] = build_template_stock_on_hand(inventory, columns)
        elif object_name == "Customers":
            outputs[object_name] = build_template_customers(customers, columns)
        elif object_name == "PartsUsage":
            outputs[object_name] = build_template_parts_usage(usage, columns, masters)
        elif object_name == "PurchaseOrders":
            outputs[object_name] = build_template_purchase_orders(
                purchase_orders if purchase_orders is not None else pd.DataFrame(),
                columns,
                masters,
                issue_tracker,
            )
        else:
            outputs[object_name] = pd.DataFrame(columns=columns)
    return outputs


def _blank_template(columns: list[str], length: int) -> pd.DataFrame:
    return pd.DataFrame({column: [""] * length for column in columns})


def _read_spi(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def _read_masters(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["SPL Master", "Items linked"])
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def _stock_audit_3y_path(cfg: PlanningConfig) -> Path:
    candidates = [
        cfg.exco_source_dir / "Stock Audit Report 3Y.txt",
        cfg.cc8_source_root / "Exco" / "April_refresh" / "Stock Audit Report 3Y.txt",
        cfg.exco_source_dir / "Stock Audit Report.txt",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


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


def _master_lookup(masters: pd.DataFrame | None) -> dict[str, str]:
    return master_lookup(masters)


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
        out["primaryPartNumber"] = main_alt.map(lambda value: _part_key(value) if str(value).strip().isdigit() else str(value).strip())
    if "isPrimary" in out.columns:
        main_alt_keys = main_alt.map(_part_key)
        out["isPrimary"] = ""
        known = main_alt_keys.astype(str).str.strip().ne("")
        out.loc[known, "isPrimary"] = (main_alt_keys[known] == item_keys[known]).map({True: "True", False: "False"})
    if "description" in out.columns:
        out["description"] = _col(parts, "ItemDescription").where(
            _col(parts, "ItemDescription").astype(str).str.strip().ne(""),
            _col(parts, "DisplayDescription"),
        )
    return out.drop_duplicates(subset=[col for col in ["SPLMaster", "PartNumber"] if col in out.columns], keep="first")


def build_template_parts_usage(
    usage: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if usage.empty:
        return pd.DataFrame(columns=columns)
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


def build_template_purchase_orders(
    purchase_orders: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
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
        out["lineCost"] = _to_number(_col(source, "LineCost"))
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


def build_parts_usage_evidence(usage: pd.DataFrame, masters: pd.DataFrame | None) -> pd.DataFrame:
    columns = [
        "Source",
        "orderNumber",
        "actualPartNumber",
        "SPLMaster",
        "Warehouse",
        "quantityUsed",
        "partsUsedDateTime",
        "EvidenceStatus",
    ]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    template = build_template_parts_usage(
        usage,
        ["orderNumber", "partCode", "Warehouse", "quantityUsed", "partsUsedDateTime"],
        masters,
    )
    if template.empty:
        return pd.DataFrame(columns=columns)
    by_part = _master_lookup(masters)
    out = pd.DataFrame(
        {
            "Source": "Stock Audit Report 3Y",
            "orderNumber": template["orderNumber"],
            "actualPartNumber": template["partCode"],
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
) -> list[Path]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    files = [
        (build_parts_usage_evidence(usage, masters), evidence_dir / "PartsUsage_SPLMaster_Evidence.csv"),
        (build_purchase_order_evidence(purchase_orders, masters, issue_tracker), evidence_dir / "PurchaseOrders_SPLMaster_Evidence.csv"),
        (purchase_order_reconciliation(purchase_orders, issue_tracker, masters), evidence_dir / "PurchaseOrders_Ticket_Reconciliation.csv"),
        (issue_tracker_evidence_rows(issue_tracker, masters), evidence_dir / "IssueTracker_Line_Evidence.csv"),
    ]
    return [_write_csv(df, path) for df, path in files]


def build_template_warehouses(warehouses: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if warehouses.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(warehouses))
    if "warehouseId" in out.columns:
        out["warehouseId"] = _col(warehouses, "WarehouseCode")
    if "warehouseDescription" in out.columns:
        out["warehouseDescription"] = _col(warehouses, "WarehouseName")
    return out.drop_duplicates(subset=["warehouseId"], keep="first") if "warehouseId" in out.columns else out


def build_template_stock_on_hand(inventory: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if inventory.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(inventory))
    if "partCode" in out.columns:
        out["partCode"] = _col(inventory, "ItemNo")
    if "warehouseCode" in out.columns:
        out["warehouseCode"] = _col(inventory, "WarehouseCode")
    if "quantityAllocated" in out.columns:
        out["quantityAllocated"] = _to_number(_first_col(inventory, ["IsCommited", "Commited"]))
    if "quantityOnHand" in out.columns:
        out["quantityOnHand"] = _to_number(_first_col(inventory, ["OnHand", "Quantity"]))
    if "quantityInbound" in out.columns:
        out["quantityInbound"] = _to_number(_first_col(inventory, ["OnOrder", "Ordered"]))
    if "uniqueId" in out.columns:
        out["uniqueId"] = ""
    subset = [col for col in ["partCode", "warehouseCode"] if col in out.columns]
    return out.drop_duplicates(subset=subset, keep="first") if subset else out


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
        elif len(populated_fields) == len(scoped_columns):
            status = "PASS"
            notes = "All in-scope template fields populated."
        else:
            status = "PARTIAL"
            blank_fields = [column for column in scoped_columns if column not in populated_fields]
            notes = "Populated: " + ", ".join(populated_fields)
            if blank_fields:
                notes += " | Needs source confirmation: " + ", ".join(blank_fields)
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
