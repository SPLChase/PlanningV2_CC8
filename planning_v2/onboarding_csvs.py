"""Generate confirmed Planning V2 onboarding CSVs from live CoCre8 SAP sources."""

from __future__ import annotations

import argparse
import re
import shutil
import zlib
from datetime import date, datetime, time, timedelta
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
from planning_v2.schemas import CONFIRMED_OUTPUT_OBJECTS, PENDING_OUTPUT_OBJECTS, TEMPLATE_OBJECT_DECISIONS
from planning_v2.sap_extracts import fetch_live_delivery_note_usage, fetch_live_purchase_orders, fetch_live_template_sources
from planning_v2.template_specs import template_columns


POPULATED_TEMPLATE_FIELDS = {
    "Parts": {
        "Primary Part": "SPI_DATA.csv:Main alternative par normalized to the part number format expected by Planning V2",
        "B_Part": "SAP Service Layer SQLQueries:OITM.ItemCode normalized as the actual CoCre8/SAP part number",
        "SPLMaster": "Reference masters.csv:SPL Master by linked item",
        "MasterKey": "Derived import key: SPLMaster when mapped, otherwise actual PartNumber",
        "PartNumber": "SAP Service Layer SQLQueries:OITM.ItemCode",
        "isPrimary": "SPI_DATA.csv:Main alternative par equals material/part number; defaults true when no main alternative is listed",
        "primaryPartNumber": "SPI_DATA.csv:Main alternative par; defaults to own part number when no main alternative is listed",
        "description": "SAP Service Layer SQLQueries:OITM.ItemName",
        "isBootStockable": "Business rule: N for all CoCre8 stock",
        "isBranchStockable": "Business rule: Y for all CoCre8 stock",
        "productClass": "Hosted Altsgen batch API: broad rollup from canonical_description",
        "productType": "Hosted Altsgen batch API: canonical_description by actual PartNumber",
        "costCategory": "Approved CoCre8 cost bands A-F from SPI-derived CoCre8 cost price",
        "isKit": "Business rule: Y when description, productType, or partType contains the word KIT; otherwise N",
        "isObsolete": "Business rule: N for all CoCre8 stock for now",
        "isTool": "Business rule: Y only for parts 1531813 and 1534363; otherwise N",
        "isExcludeFromReplenishment": "Business rule: N for all CoCre8 stock for now",
        "purchaseLeadTimeDays": "Business rule: default 3 days; 180 days when description contains BBU",
        "isCritical": "Business rule: Yes for all CoCre8 stock",
        "partType": "Hosted Altsgen batch API:commodity_type by actual PartNumber",
    },
    "Warehouses": {
        "warehouseId": "SAP Service Layer Warehouses:WarehouseCode",
        "addressId": "Business rule: FUJ CT shared-address warehouses use FUJ CT; all other CoCre8 warehouses use FUJITSU for v1 location planning",
        "returnWarehouseId": "Manual fill workbook:returnWarehouseId",
        "supplyWarehouseId": "Manual fill workbook:supplyWarehouseId",
        "warehouseTypeId": "Manual fill workbook:warehouseTypeId",
        "warehouseDescription": "SAP Service Layer Warehouses:WarehouseName",
        "isPrimary": "Business rule: Y only for FUJITSU; N for all other CoCre8 warehouses",
        "isReplenishable": "Manual fill workbook:isReplenishable",
        "isBranchStockable": "Manual fill workbook:isBranchStockable",
        "isRemote": "Manual fill workbook:isRemote",
        "warehouseStatusId": "Manual fill workbook:isObsolete inverted to is_active flag (Y active, N obsolete)",
    },
    "WarehouseStockOnHand": {
        "Ws Part": "Planning V2 UI alias for warehouse stock part id; same SAP part number as partCode",
        "partCode": "SAP Service Layer SQLQueries:OITW.ItemCode",
        "warehouseCode": "SAP Service Layer SQLQueries:OITW.WhsCode",
        "quantityAllocated": "SAP Service Layer SQLQueries:OITW.IsCommited",
        "quantityOnHand": "SAP Service Layer SQLQueries:OITW.OnHand",
        "quantityInbound": "SAP Service Layer SQLQueries:OITW.OnOrder",
        "quantityOutbound": "Business rule: 0 for CoCre8 MVP; CoCre8 stock is issued directly rather than staged as outbound stock",
        "uniqueId": "Derived stable integer from MinStock-style RowKey part|SPLMaster|warehouse",
    },
    "Vendors": {
        "vendorId": "SAP Service Layer SQLQueries:OPOR.CardCode",
        "Description": "SAP Service Layer SQLQueries:OCRD.CardName joined from PO vendor",
        "isActive": "SAP Service Layer SQLQueries:OCRD.validFor joined from PO vendor",
    },
    "Customers": {
        "customerId": "Generated stable CC8CUST-* id from normalized downstream customer name because SAP Delivery Notes use CardCode FTS002 for CoCre8, not the end customer.",
        "customerName": "HelpDesk issue tracker: distinct Customer values",
        "Description": "HelpDesk issue tracker: distinct Customer values when the raw template uses Description",
        "assignAnySkill": "Business rule: Y for HelpDesk-derived customer draft",
        "isActive": "Business rule: Y for HelpDesk-derived customer draft",
        "stdResponseTime": "HelpDesk issue tracker:SLA parsed to numeric hours; NBD treated as 16 business hours pending client confirmation",
        "stdRepairTime": "HelpDesk issue tracker:SLA parsed to numeric hours for Recovery/Repair/Resolve-type SLAs; NBD treated as 16 business hours pending client confirmation",
    },
    "Addresses": {
        "externalAddressId": "Generated stable CC8ADDR-* id from generated customer id + normalized SAP Delivery Note ShipToAddress",
        "customerExternalId": "Generated stable CC8CUST-* id matching Customers.customerId",
        "addressLine1": "SAP Service Layer SQLQueries:ODLN.Address2/ShipToAddress first usable line",
        "addressLine2": "SAP Service Layer SQLQueries:ODLN.Address2/ShipToAddress second usable line",
        "addressLine3": "SAP Service Layer SQLQueries:ODLN.Address2/ShipToAddress third and remaining usable lines",
        "city": "SAP DN ShipToAddress city keyword, collection warehouse location, or MinStock/Exco warehouse location map",
        "stateProvince": "Derived from inferred city/location code",
        "countryCode": "ZA by default; SZ for Royal Swazi/RSSC/Swazi/Eswatini evidence",
        "latitude": "City/location-level coordinate estimate",
        "longitude": "City/location-level coordinate estimate",
        "timeZone": "South Africa Standard Time",
    },
    "PartsUsage": {
        "orderNumber": "SAP Service Layer SQLQueries:ODLN.DocNum/DeliveryNoteNumber as the SAP delivery note id",
        "requestId": "SAP Service Layer SQLQueries:ODLN.NumAtCard or parsed Call Nr from ODLN.Comments as the helpdesk call/ticket id where present",
        "OrderType": "Business rule: service_order for all CoCre8 usage",
        "customerCompanyCode": "SAP Service Layer SQLQueries:parsed Customer from ODLN.Comments where present",
        "orderStartDatetime": "HelpDesk issue tracker Created date by unambiguous call-number match",
        "orderStatus": "HelpDesk issue tracker Status by unambiguous call-number match",
        "Pu Part": "Planning V2 UI alias for parts usage part id; same actual delivered SAP part as partCode",
        "Part Code": "Planning V2 UI alias for part id/bpart join; same actual delivered SAP part as partCode",
        "partCode": "SAP Service Layer SQLQueries:DLN1.ItemCode actual delivered part",
        "serialNumber": "SAP Service Layer SQLQueries:parsed Serial number from ODLN.Comments where present",
        "quantityUsed": "SAP Service Layer SQLQueries:DLN1.Quantity",
        "partsUsedDateTime": "SAP Service Layer SQLQueries:ODLN.DocDate",
        "Pu Warehouse Code": "Planning V2 UI alias for parts usage warehouse id; SAP Service Layer SQLQueries:DLN1.WhsCode",
        "Pu Warehouse": "Planning V2 UI alias for warehouse id; SAP Service Layer SQLQueries:DLN1.WhsCode",
        "Warehouse": "MinStock3/Exco warehouse lookup:WarehouseName by SAP DLN1.WhsCode",
        "Warehouse Code": "SAP Service Layer SQLQueries:DLN1.WhsCode",
        "deviceSerialNumber": "SAP Service Layer SQLQueries:parsed Serial number from ODLN.Comments where present",
        "Master": "Reference masters.csv:SPL Master by used part",
        "MasterKey": "Derived import key: Master when mapped, otherwise actual delivered partCode",
    },
    "ServiceOrder": {
        "orderNumber": "Spares issued report:Del No. as the SAP delivery note id where available; falls back to call/ticket number for helpdesk-only rows",
        "RequestID": "Spares issued report:Cust Ord No normalised to call/ticket number",
        "location": "HelpDesk issue tracker:DeliveryCity by call number; falls back to report customer label",
        "actualEta": "Spares issued report:Del Date + Del Time, representing delivery note issued time",
        "slaEtaClock": "HelpDesk issue tracker:SLA target hours from call received to SLA deadline",
        "slaResolveClock": "HelpDesk issue tracker:SLA target hours for Recovery/Repair/Resolve-type SLAs only",
        "slaResolveDateTime": "HelpDesk issue tracker:SLA-derived deadline for Recovery/Repair/Resolve-type SLAs",
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
        "movementType": "Template example default: Internal_move for posted warehouse transfer rows",
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
    "PartTypes": {
        "partType": "Hosted Altsgen batch API:commodity_type for CoCre8 parts",
        "partTypeDescription": "Hosted Altsgen batch API:spec_summary.description or humanized commodity_type",
        "isReworkable": "Business rule: N/No for all CoCre8 part types",
    },
    "WarehouseExclusions": {
        "Secondary TO": "Business rule: destination warehouse in a prohibited warehouse-to-warehouse stock-pooling route",
        "Secondary FROM": "Business rule: source warehouse in a prohibited warehouse-to-warehouse stock-pooling route",
        "Cross TO": "Business rule: destination warehouse in a prohibited warehouse-to-warehouse stock-pooling route",
        "Cross FROM": "Business rule: source warehouse in a prohibited warehouse-to-warehouse stock-pooling route",
        "Primary TO": "Left blank for CoCre8 v1; current rule only limits warehouse-to-warehouse stock pooling",
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
    ("Parts", "isSmallPart"),
    ("PurchaseOrders", "customerId"),
    ("PurchaseOrders", "requestTicketDateTime"),
    ("PurchaseOrders", "isResolved"),
    ("ServiceOrder", "eta"),
    ("ServiceOrder", "actualResolveDateTime"),
    ("ServiceOrder", "recallDateTime"),
    ("ServiceOrder", "actualRecallDateTime"),
    ("ServiceOrder", "slaFailureCode"),
    ("ServiceOrder", "slaEtaHit"),
    ("InventoryTransfers", "addressId"),
    ("InventoryTransfers", "Bpart"),
    ("InventoryTransfers", "shipListCode"),
    ("Warehouses", "nodeId"),
    ("Warehouses", "isRepairWarehouse"),
    ("Warehouses", "isBootStockable"),
    ("WarehouseStockOnHand", "inventoryType"),
    ("Customers", "customerGroupId"),
    ("Customers", "dseSlaCost"),
    ("Customers", "dseSlaRevenue"),
    ("Addresses", "nodeId"),
}

ROW_REQUIRED_TEMPLATE_FIELDS = {
    "PartCost": ["partCode", "cost", "currencyCode", "averageCost"],
    "Parts": [
        "PartNumber",
        "isPrimary",
        "primaryPartNumber",
        "description",
        "isBootStockable",
        "isBranchStockable",
        "productClass",
        "productType",
        "partType",
        "isKit",
        "isObsolete",
        "isTool",
        "isExcludeFromReplenishment",
        "purchaseLeadTimeDays",
        "isCritical",
    ],
}

EXCLUDED_WAREHOUSE_CODES = {"CHOICE", "GCJRMA", "MXT"}
EXCLUDED_WAREHOUSE_PREFIXES = ("CHL",)
EXCLUDED_WAREHOUSE_NAME_PATTERNS = ("CHOICE LOGISTICS", "CHOICE RMA", "MAXTEC")

WAREHOUSE_INTERCHANGEABILITY_POOLS = {
    "main_non_dedicated": {"FUJITSU", "FUJ CT", "FSCGREEN", "FSCGRNCT"},
    "massmart": {"FUJMSM C", "FUJMSM J", "FUJMSVCJ"},
    "royal_swazi": {"FUJ RSSC", "FUJSWBAN"},
}

FUJ_CT_ADDRESS_WAREHOUSES = {
    "BNI CT",
    "FSCGRNCT",
    "FUJ CT",
    "FUJ IEC",
    "FUJOM CT",
    "FUJSANCT",
    "FUJWO CT",
}

WAREHOUSE_ADDRESS_ROWS = [
    {
        "externalAddressId": "FUJITSU",
        "customerExternalId": "",
        "CustomerName": "FUJITSU warehouse",
        "RawShipToAddress": "5 Star Junction\nCnr Beyers Naude & Juice St\nHoneydew, Johannesburg",
        "addressLine1": "5 Star Junction",
        "addressLine2": "Cnr Beyers Naude & Juice St",
        "addressLine3": "Honeydew, Johannesburg",
        "city": "Johannesburg",
        "stateProvince": "Gauteng",
        "countryCode": "ZA",
        "latitude": "-26.0739306",
        "longitude": "27.9220694",
        "timeZone": "South Africa Standard Time",
        "WarehouseCode": "FUJITSU",
        "LocationEvidence": "User-provided warehouse address and GPS coordinates",
    },
    {
        "externalAddressId": "FUJ CT",
        "customerExternalId": "",
        "CustomerName": "FUJ CT warehouse",
        "RawShipToAddress": "Unit L4, Centurion Business Park\nCnr Bosmansdam & Democracy Road\nMontague Gardens, Cape Town",
        "addressLine1": "Unit L4, Centurion Business Park",
        "addressLine2": "Cnr Bosmansdam & Democracy Road",
        "addressLine3": "Montague Gardens, Cape Town",
        "city": "Cape Town",
        "stateProvince": "Western Cape",
        "countryCode": "ZA",
        "latitude": "-33.8795028",
        "longitude": "18.5079583",
        "timeZone": "South Africa Standard Time",
        "WarehouseCode": "FUJ CT",
        "LocationEvidence": "User-provided warehouse address and GPS coordinates",
    },
]

APPROVED_COST_CATEGORY_BANDS = [
    ("A", 58.70),
    ("B", 165.65),
    ("C", 282.98),
    ("D", 441.62),
    ("E", 900.01),
    ("F", float("inf")),
]


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def read_warehouse_location_lookup(cfg: PlanningConfig) -> pd.DataFrame:
    paths = [
        cfg.minstock3_dir / "customers.csv",
        cfg.exco_source_dir / "warehouse_dimension.csv",
        cfg.exco_source_dir / "FactWarehouses.csv",
    ]
    frames: list[pd.DataFrame] = []
    for path in paths:
        frame = _read_csv(path)
        if frame.empty:
            continue
        warehouse = _first_col(frame, ["WarehouseCode", "Warehouse Code", "warehouseCode"])
        location = _first_col(frame, ["Location", "LocationKey", "location"])
        name = _first_col(frame, ["WarehouseName", "Warehouse Name", "warehouseDescription"], "")
        out = pd.DataFrame(
            {
                "WarehouseCode": warehouse.map(_clean_text),
                "Location": location.map(_clean_text),
                "WarehouseName": name.map(_clean_text),
            }
        )
        out = out[out["WarehouseCode"].astype(str).str.strip().ne("")]
        if not out.empty:
            frames.append(out)
    if not frames:
        return pd.DataFrame(columns=["WarehouseCode", "Location", "WarehouseName"])
    combined = pd.concat(frames, ignore_index=True, sort=False).fillna("")
    combined["WarehouseKey"] = combined["WarehouseCode"].str.upper().str.strip()
    return combined.drop_duplicates(subset=["WarehouseKey"], keep="first").reset_index(drop=True)


def _warehouse_name_lookup(warehouse_locations: pd.DataFrame | None) -> dict[str, str]:
    if warehouse_locations is None or warehouse_locations.empty:
        return {}
    work = warehouse_locations.copy()
    code = _first_col(work, ["WarehouseCode", "warehouseCode", "warehouseId"]).map(_clean_text)
    name = _first_col(work, ["WarehouseName", "Warehouse Name", "warehouseDescription"]).map(_clean_text)
    lookup = pd.DataFrame({"WarehouseCode": code, "WarehouseName": name})
    lookup = lookup[lookup["WarehouseCode"].astype(str).str.strip().ne("")]
    lookup = lookup[lookup["WarehouseName"].astype(str).str.strip().ne("")]
    lookup["WarehouseKey"] = lookup["WarehouseCode"].str.upper().str.strip()
    return lookup.drop_duplicates(subset=["WarehouseKey"], keep="first").set_index("WarehouseKey")["WarehouseName"].to_dict()


def _combine_warehouse_name_sources(*frames: pd.DataFrame | None) -> pd.DataFrame:
    rows = []
    for frame in frames:
        if frame is None or frame.empty:
            continue
        code = _first_col(frame, ["WarehouseCode", "warehouseCode", "warehouseId"]).map(_clean_text)
        name = _first_col(frame, ["WarehouseName", "Warehouse Name", "warehouseDescription"]).map(_clean_text)
        location = _first_col(frame, ["Location", "LocationKey", "location"]).map(_clean_text)
        rows.append(pd.DataFrame({"WarehouseCode": code, "WarehouseName": name, "Location": location}))
    if not rows:
        return pd.DataFrame(columns=["WarehouseCode", "WarehouseName", "Location"])
    combined = pd.concat(rows, ignore_index=True, sort=False).fillna("")
    combined = combined[combined["WarehouseCode"].astype(str).str.strip().ne("")]
    combined["WarehouseKey"] = combined["WarehouseCode"].str.upper().str.strip()
    return combined.drop_duplicates(subset=["WarehouseKey"], keep="first").drop(columns=["WarehouseKey"]).reset_index(drop=True)


def read_altsgen_part_type_evidence(output_parent: Path) -> pd.DataFrame:
    candidates = [
        output_parent / "review_evidence" / "Altsgen_PartType_Evidence.csv",
        Path("data") / "output" / "review_evidence" / "Altsgen_PartType_Evidence.csv",
    ]
    for path in candidates:
        frame = _read_csv(path)
        if not frame.empty:
            return frame
    return pd.DataFrame()


def _write_csv(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0.0)


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat", "null", "<na>"} else text


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


def _select_parts_usage_source(live_delivery_notes: pd.DataFrame, stock_audit: pd.DataFrame) -> pd.DataFrame:
    """Use live SAP DNs for current usage; fall back to the manual stock audit only if SAP is unavailable."""

    return live_delivery_notes if not live_delivery_notes.empty else stock_audit


def generate_onboarding_csvs(cfg: PlanningConfig, out_dir: Path) -> list[Path]:
    csv_dir = out_dir
    template_dir = out_dir.parent / "investigation_templates"
    csv_dir.mkdir(parents=True, exist_ok=True)
    template_dir.mkdir(parents=True, exist_ok=True)

    parts, warehouses, inventory = fetch_live_template_sources(cfg)
    purchase_orders = fetch_live_purchase_orders(cfg)
    usage_context = fetch_live_delivery_note_usage(cfg)
    stock_audit = _read_stock_audit(_stock_audit_3y_path(cfg))
    parts_usage_source = _select_parts_usage_source(usage_context, stock_audit)
    usage = usage_context if not usage_context.empty else stock_audit
    stock_flow = stock_audit
    customers = pd.DataFrame()
    masters = _read_masters(cfg.reference_dir / "masters.csv")
    spi = _read_spi(cfg.exco_source_dir / "SPI_DATA.csv")
    spi_cost_history = _read_spi_cost_history(cfg.reference_dir / "SPI_Historical")
    combined_spi_costs = _combine_spi_cost_sources(spi, spi_cost_history)
    issue_tracker = read_issue_tracker(cfg.issue_tracker_csv)
    spares_issued = read_spares_issued_report(cfg.spares_issued_report)
    manual_warehouses = read_manual_warehouse_fill(cfg)
    warehouse_locations = read_warehouse_location_lookup(cfg)
    altsgen_part_type_evidence = read_altsgen_part_type_evidence(out_dir.parent)
    warehouses, inventory, usage, purchase_orders, stock_flow, manual_warehouses = filter_excluded_warehouse_sources(
        warehouses,
        inventory,
        usage,
        purchase_orders,
        stock_flow,
        manual_warehouses,
    )
    parts_usage_source = (
        _filter_frame_excluded_warehouses(parts_usage_source, "Whse")
        if "Whse" in parts_usage_source.columns
        else _filter_frame_excluded_warehouses(parts_usage_source, "WarehouseCode")
    )

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
        spares_issued,
        manual_warehouses,
        spi_cost_history,
        altsgen_part_type_evidence,
        warehouse_locations,
        parts_usage_source,
        usage_context,
    )

    for object_name, df in outputs.items():
        written.append(_write_csv(df, csv_dir / f"{object_name}.csv"))

    validation = validate_template_outputs(csv_dir, outputs, templates)
    written.append(_write_csv(validation, out_dir.parent / "validation_summary.csv"))
    written.extend(sync_upload_ready_package(csv_dir, outputs, validation, out_dir.parent / "upload_ready"))
    written.extend(
        write_review_evidence(
            out_dir.parent / "review_evidence",
            usage,
            purchase_orders,
            issue_tracker,
            masters,
            manual_warehouses,
            warehouses,
            parts,
            combined_spi_costs,
            parts_usage_source,
            usage_context,
            warehouse_locations,
        )
    )
    return written


def sync_upload_ready_package(
    csv_dir: Path,
    outputs: dict[str, pd.DataFrame],
    validation: pd.DataFrame,
    ready_dir: Path,
) -> list[Path]:
    """Write a clean upload package containing only current PASS template CSVs."""

    ready_dir.mkdir(parents=True, exist_ok=True)
    for child in ready_dir.glob("*.csv"):
        child.unlink()
    for child in ready_dir.glob("*.txt"):
        child.unlink()

    written: list[Path] = []
    pass_objects = set(
        validation.loc[validation["Status"].astype(str).str.upper().eq("PASS"), "Object"].astype(str)
    )
    for object_name in sorted(pass_objects):
        source = csv_dir / f"{object_name}.csv"
        if not source.exists() or object_name not in outputs:
            continue
        destination = ready_dir / source.name
        shutil.copy2(source, destination)
        written.append(destination)

    manifest = validation[validation["Object"].astype(str).isin(pass_objects)].copy()
    written.append(_write_csv(manifest, ready_dir / "upload_manifest.csv"))
    readme = ready_dir / "README_DO_NOT_USE_TEMPLATE_ONBOARDING_CSVS.txt"
    readme.write_text(
        "Use the CSV files in this upload_ready folder for Planning V2 ingestion.\n"
        "Do not use data/output/template_onboarding_csvs; that folder contains stale header/sample outputs from early exploration.\n"
        "Generated from live SAP Service Layer plus approved local exception sources.\n",
        encoding="utf-8",
    )
    written.append(readme)
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
    spares_issued: pd.DataFrame | None = None,
    manual_warehouses: pd.DataFrame | None = None,
    spi_cost_history: pd.DataFrame | None = None,
    altsgen_part_type_evidence: pd.DataFrame | None = None,
    warehouse_locations: pd.DataFrame | None = None,
    parts_usage_source: pd.DataFrame | None = None,
    delivery_note_context: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    outputs: dict[str, pd.DataFrame] = {}
    for object_name, columns in templates.items():
        if object_name == "Parts":
            outputs[object_name] = build_template_parts(parts, columns, spi, masters, altsgen_part_type_evidence)
        elif object_name == "Warehouses":
            outputs[object_name] = build_template_warehouses(warehouses, columns, manual_warehouses)
        elif object_name == "WarehouseStockOnHand":
            outputs[object_name] = build_template_stock_on_hand(inventory, columns, masters)
        elif object_name == "Customers":
            outputs[object_name] = build_template_customers(customers, columns, issue_tracker, usage)
        elif object_name == "Addresses":
            outputs[object_name] = build_template_addresses(usage, columns, warehouse_locations)
        elif object_name == "Vendors":
            outputs[object_name] = build_template_vendors(
                purchase_orders if purchase_orders is not None else pd.DataFrame(),
                columns,
            )
        elif object_name == "PartsUsage":
            outputs[object_name] = build_template_parts_usage(
                parts_usage_source if parts_usage_source is not None and not parts_usage_source.empty else usage,
                columns,
                masters,
                issue_tracker,
                _combine_warehouse_name_sources(warehouses, warehouse_locations),
                delivery_note_context,
            )
        elif object_name == "ServiceOrder":
            outputs[object_name] = build_template_service_orders(
                spares_issued if spares_issued is not None else pd.DataFrame(),
                columns,
                issue_tracker,
            )
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
        elif object_name == "PartTypes":
            outputs[object_name] = build_template_part_types(
                altsgen_part_type_evidence if altsgen_part_type_evidence is not None else pd.DataFrame(),
                columns,
            )
        elif object_name == "InventoryTransfers":
            outputs[object_name] = build_template_inventory_transfers(stock_flow, columns)
        elif object_name == "WarehouseExclusions":
            outputs[object_name] = build_template_warehouse_exclusions(warehouses, columns, manual_warehouses)
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


def read_spares_issued_report(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        book = pd.ExcelFile(path)
    except Exception:
        return pd.DataFrame()
    sheet_name = next((name for name in book.sheet_names if "customer" in name.lower()), "")
    if not sheet_name:
        return pd.DataFrame()
    try:
        return pd.read_excel(book, sheet_name=sheet_name, dtype=str).fillna("")
    except Exception:
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


def filter_excluded_warehouse_sources(
    warehouses: pd.DataFrame,
    inventory: pd.DataFrame,
    usage: pd.DataFrame,
    purchase_orders: pd.DataFrame,
    stock_flow: pd.DataFrame,
    manual_warehouses: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return (
        _filter_frame_excluded_warehouses(warehouses, "WarehouseCode", "WarehouseName"),
        _filter_frame_excluded_warehouses(inventory, "WarehouseCode"),
        _filter_frame_excluded_warehouses(usage, "WarehouseCode"),
        _filter_frame_excluded_warehouses(purchase_orders, "ToWarehouseId"),
        _filter_frame_excluded_warehouses(stock_flow, "Whse"),
        _filter_frame_excluded_warehouses(manual_warehouses if manual_warehouses is not None else pd.DataFrame(), "warehouseId", "warehouseDescription"),
    )


def _filter_frame_excluded_warehouses(df: pd.DataFrame, code_column: str, name_column: str | None = None) -> pd.DataFrame:
    if df.empty or code_column not in df.columns:
        return df
    mask = _excluded_warehouse_mask(df[code_column], df[name_column] if name_column and name_column in df.columns else None)
    return df[~mask].copy().reset_index(drop=True)


def _excluded_warehouse_mask(code_values: pd.Series, name_values: pd.Series | None = None) -> pd.Series:
    codes = code_values.map(_clean_text).str.upper()
    mask = codes.isin(EXCLUDED_WAREHOUSE_CODES)
    for prefix in EXCLUDED_WAREHOUSE_PREFIXES:
        mask = mask | codes.str.startswith(prefix)
    if name_values is not None:
        names = name_values.map(_clean_text).str.upper()
        for pattern in EXCLUDED_WAREHOUSE_NAME_PATTERNS:
            mask = mask | names.str.contains(re.escape(pattern), regex=True)
    return mask


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


def _parse_datetime_value(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text or text in {"-", "0"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(tzinfo=None)


def _report_datetime(date_value: object, time_value: object) -> datetime | None:
    date_text = str(date_value or "").strip()
    if not date_text or date_text == "-":
        return None
    parsed_date = pd.to_datetime(date_text, format="%d/%m/%Y", errors="coerce")
    if pd.isna(parsed_date):
        parsed_date = pd.to_datetime(date_text, dayfirst=True, errors="coerce")
    if pd.isna(parsed_date):
        return None
    parsed_time = _report_time(time_value)
    return datetime.combine(parsed_date.to_pydatetime().date(), parsed_time)


def _report_time(value: object) -> time:
    text = str(value or "").strip()
    if not text or text == "-":
        return time(0, 0)
    text = re.sub(r"\.0$", "", text)
    if ":" in text:
        parsed = pd.to_datetime(text, errors="coerce")
        if not pd.isna(parsed):
            return parsed.to_pydatetime().time().replace(second=0, microsecond=0)
    digits = re.sub(r"\D", "", text)
    if not digits:
        return time(0, 0)
    digits = digits.zfill(4)[-4:]
    hour = min(int(digits[:2]), 23)
    minute = min(int(digits[2:]), 59)
    return time(hour, minute)


def _format_datetime(value: datetime | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _first_datetime(values: pd.Series) -> datetime | None:
    cleaned = [value for value in values if isinstance(value, datetime) and not pd.isna(value)]
    if not cleaned:
        return None
    return min(cleaned)


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
    if tokens:
        return "|".join((token.lstrip("0") or "0") for token in tokens)
    return re.sub(r"\s+", " ", text)


def _helpdesk_by_call(issue_tracker: pd.DataFrame | None) -> dict[str, dict[str, str]]:
    if issue_tracker is None or issue_tracker.empty:
        return {}
    work = issue_tracker.copy()
    columns = [
        "Call Number",
        "Created",
        "Status",
        "SLA",
        "DeliveryCity",
        "Customer",
        "CustomerNormalized",
    ]
    for column in columns:
        if column not in work.columns:
            work[column] = ""
    work["CallMatchKey"] = work["Call Number"].map(_call_match_key)
    work = work[work["CallMatchKey"].astype(str).str.strip().ne("")]
    if work.empty:
        return {}
    work = work.drop_duplicates(subset=["CallMatchKey"], keep="first")
    return work.set_index("CallMatchKey")[columns[1:]].to_dict("index")


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


def _delivery_note_context_by_number(delivery_note_context: pd.DataFrame | None) -> dict[str, dict[str, object]]:
    if delivery_note_context is None or delivery_note_context.empty or "DeliveryNoteNumber" not in delivery_note_context.columns:
        return {}
    work = _filtered_sap_delivery_note_rows(delivery_note_context)
    if work.empty:
        return {}
    work["DeliveryNoteNumberKey"] = work["DeliveryNoteNumber"].map(_clean_text)
    work = work[work["DeliveryNoteNumberKey"].ne("")]
    if work.empty:
        return {}
    work = work.drop_duplicates(subset=["DeliveryNoteNumberKey"], keep="first")
    return work.set_index("DeliveryNoteNumberKey").to_dict("index")


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


def _cost_category(value: object) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number) or float(number) <= 0:
        return ""
    for category, upper_bound in APPROVED_COST_CATEGORY_BANDS:
        if float(number) <= upper_bound:
            return category
    return ""


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


def _part_type_lookup(altsgen_evidence: pd.DataFrame | None) -> dict[str, str]:
    if altsgen_evidence is None or altsgen_evidence.empty:
        return {}
    required = {"PartNumber", "status", "partType"}
    if not required.issubset(altsgen_evidence.columns):
        return {}
    source = altsgen_evidence.copy()
    source["PartKey"] = source["PartNumber"].map(_part_key)
    source["Status"] = source["status"].map(_clean_text).str.lower()
    source["PartType"] = source["partType"].map(_clean_text)
    source = source[
        source["PartKey"].str.strip().ne("")
        & source["Status"].eq("ok")
        & source["PartType"].str.strip().ne("")
        & source["PartType"].str.lower().ne("unknown")
    ].copy()
    if source.empty:
        return {}
    source = source.drop_duplicates(subset=["PartKey"], keep="last")
    return dict(zip(source["PartKey"], source["PartType"], strict=False))


def _canonical_description_lookup(altsgen_evidence: pd.DataFrame | None) -> dict[str, str]:
    if altsgen_evidence is None or altsgen_evidence.empty:
        return {}
    if "canonicalDescription" not in altsgen_evidence.columns:
        return {}
    return _altsgen_value_lookup(altsgen_evidence, "canonicalDescription")


def _product_class_lookup(altsgen_evidence: pd.DataFrame | None) -> dict[str, str]:
    if altsgen_evidence is None or altsgen_evidence.empty:
        return {}
    canonical = _canonical_description_lookup(altsgen_evidence)
    part_types = _part_type_lookup(altsgen_evidence)
    keys = set(canonical) | set(part_types)
    return {
        key: product_class
        for key in keys
        if (product_class := _product_class_from_altsgen(canonical.get(key, ""), part_types.get(key, "")))
    }


def _altsgen_value_lookup(altsgen_evidence: pd.DataFrame, value_column: str) -> dict[str, str]:
    required = {"PartNumber", "status", value_column}
    if not required.issubset(altsgen_evidence.columns):
        return {}
    source = altsgen_evidence.copy()
    source["PartKey"] = source["PartNumber"].map(_part_key)
    source["Status"] = source["status"].map(_clean_text).str.lower()
    source["Value"] = source[value_column].map(_clean_text)
    source = source[
        source["PartKey"].str.strip().ne("")
        & source["Status"].eq("ok")
        & source["Value"].str.strip().ne("")
    ].copy()
    if source.empty:
        return {}
    source = source.drop_duplicates(subset=["PartKey"], keep="last")
    return dict(zip(source["PartKey"], source["Value"], strict=False))


def _product_class_from_altsgen(canonical_description: object, part_type: object = "") -> str:
    text = f"{_clean_text(canonical_description)} {_clean_text(part_type)}".lower()
    if not text.strip():
        return ""
    if any(token in text for token in ["hdd", "ssd", "disk", "drive", "storage", "tape", "raid cache"]):
        return "STORAGE"
    if any(token in text for token in ["ram", "memory", "dimm", "sodimm"]):
        return "MEMORY"
    if any(token in text for token in ["psu", "power supply", "adapter", "battery", "bbu"]):
        return "POWER"
    if any(token in text for token in ["motherboard", "mainboard", "backplane", "io board", "controller board"]):
        return "BOARD"
    if any(token in text for token in ["cpu", "processor", "heatsink"]):
        return "CPU"
    if any(token in text for token in ["fan", "cooling"]):
        return "COOLING"
    if any(token in text for token in ["cable", "connector", "transceiver", "sfp"]):
        return "CONNECTIVITY"
    if any(token in text for token in ["keyboard", "lcd", "screen", "panel", "cover", "hinge", "bezel", "touchpad"]):
        return "DISPLAY_INPUT"
    if any(token in text for token in ["chassis", "enclosure", "caddy", "tray", "bracket", "rail"]):
        return "MECHANICAL"
    return "OTHER"


TOOL_PART_KEYS = {"1531813", "1534363"}
KIT_KEYWORD_RE = re.compile(r"(?<![A-Za-z0-9])kit(?![A-Za-z0-9])", re.IGNORECASE)


def _kit_flags(description: pd.Series, product_type: pd.Series, part_type: pd.Series) -> pd.Series:
    combined = (
        description.fillna("").astype(str)
        + " "
        + product_type.fillna("").astype(str)
        + " "
        + part_type.fillna("").astype(str)
    )
    return combined.str.contains(KIT_KEYWORD_RE, na=False).map({True: "Y", False: "N"})


def _tool_flags(part_numbers: pd.Series) -> pd.Series:
    return part_numbers.map(_part_key).isin(TOOL_PART_KEYS).map({True: "Y", False: "N"})


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
    altsgen_evidence: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if parts.empty:
        return pd.DataFrame(columns=columns)
    spi = spi if spi is not None else pd.DataFrame()
    main_alt_by_part = _spi_main_alt_lookup(spi)
    master_by_part = _master_lookup(masters)
    part_type_by_part = _part_type_lookup(altsgen_evidence)
    product_type_by_part = _canonical_description_lookup(altsgen_evidence)
    product_class_by_part = _product_class_lookup(altsgen_evidence)
    cost_by_part = _spi_cocre8_cost_lookup(spi)
    out = _blank_template(columns, len(parts))
    item_keys = _col(parts, "ItemNo").map(_part_key)
    main_alt = item_keys.map(main_alt_by_part).fillna("")
    primary = main_alt.map(lambda value: _part_key(value) if str(value).strip().isdigit() else str(value).strip())
    primary = primary.where(primary.astype(str).str.strip().ne(""), item_keys)
    if "Primary Part" not in out.columns:
        out.insert(0, "Primary Part", primary)
    else:
        out["Primary Part"] = primary
    if "B_Part" not in out.columns:
        insert_at = 1 if "Primary Part" in out.columns else 0
        out.insert(insert_at, "B_Part", item_keys)
    else:
        out["B_Part"] = item_keys
    spl_master = item_keys.map(master_by_part).fillna("")
    if "SPLMaster" in out.columns:
        out["SPLMaster"] = spl_master
    _add_master_key_column(out, spl_master, item_keys, after="SPLMaster" if "SPLMaster" in out.columns else "B_Part")
    if "PartNumber" in out.columns:
        out["PartNumber"] = item_keys
    if "primaryPartNumber" in out.columns:
        out["primaryPartNumber"] = primary
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
        out["description"] = description.where(description.astype(str).str.strip().ne(""), "No Description")
    if "isBootStockable" in out.columns:
        out["isBootStockable"] = "N"
    if "isBranchStockable" in out.columns:
        out["isBranchStockable"] = "Y"
    if "productClass" in out.columns:
        out["productClass"] = item_keys.map(product_class_by_part).fillna("")
        out["productClass"] = out["productClass"].where(out["productClass"].astype(str).str.strip().ne(""), "OTHER")
    if "productType" in out.columns:
        out["productType"] = item_keys.map(product_type_by_part).fillna("")
        out["productType"] = out["productType"].where(out["productType"].astype(str).str.strip().ne(""), "Unknown Component")
    if "costCategory" in out.columns:
        out["costCategory"] = item_keys.map(cost_by_part).map(_cost_category)
    if "isObsolete" in out.columns:
        out["isObsolete"] = "N"
    if "isExcludeFromReplenishment" in out.columns:
        out["isExcludeFromReplenishment"] = "N"
    if "purchaseLeadTimeDays" in out.columns:
        desc = out["description"].astype(str)
        out["purchaseLeadTimeDays"] = desc.str.contains("BBU", case=False, na=False).map({True: 180, False: 3})
    if "isCritical" in out.columns:
        out["isCritical"] = "Yes"
    if "partType" in out.columns:
        out["partType"] = item_keys.map(part_type_by_part).fillna("")
        out["partType"] = out["partType"].where(out["partType"].astype(str).str.strip().ne(""), "unknown_component")
    kit_product_type = out["productType"] if "productType" in out.columns else item_keys.map(product_type_by_part).fillna("")
    kit_part_type = out["partType"] if "partType" in out.columns else item_keys.map(part_type_by_part).fillna("")
    if "isKit" in out.columns:
        out["isKit"] = _kit_flags(out["description"] if "description" in out.columns else _col(parts, "ItemDescription"), kit_product_type, kit_part_type)
    if "isTool" in out.columns:
        out["isTool"] = _tool_flags(_col(parts, "ItemNo"))
    out = out.drop_duplicates(keep="first")
    return out.drop_duplicates(subset=[col for col in ["SPLMaster", "PartNumber"] if col in out.columns], keep="first")


def build_template_parts_usage(
    usage: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
    warehouse_locations: pd.DataFrame | None = None,
    delivery_note_context: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if usage.empty:
        return pd.DataFrame(columns=columns)
    if {"DeliveryNoteNumber", "ItemNo", "WarehouseCode"}.issubset(usage.columns):
        return _build_template_parts_usage_from_sap_delivery_notes(usage, columns, masters, issue_tracker, warehouse_locations)
    return _build_template_parts_usage_from_stock_audit(
        usage,
        columns,
        masters,
        issue_tracker,
        warehouse_locations,
        delivery_note_context,
    )


def _set_or_insert_column(
    frame: pd.DataFrame,
    column: str,
    values: pd.Series,
    *,
    before: str | None = None,
    after: str | None = None,
) -> None:
    if column in frame.columns:
        frame[column] = values
        return
    if before and before in frame.columns:
        frame.insert(frame.columns.get_loc(before), column, values)
        return
    if after and after in frame.columns:
        frame.insert(frame.columns.get_loc(after) + 1, column, values)
        return
    frame[column] = values


def _add_parts_usage_ui_aliases(out: pd.DataFrame, part_code: pd.Series, warehouse_code: pd.Series) -> None:
    _set_or_insert_column(out, "Pu Part", part_code, before="partCode")
    _set_or_insert_column(out, "Part Code", part_code, after="Pu Part")
    _set_or_insert_column(out, "Pu Warehouse Code", warehouse_code, before="Warehouse")
    _set_or_insert_column(out, "Pu Warehouse", warehouse_code, after="Pu Warehouse Code")


def _add_master_key_column(
    out: pd.DataFrame,
    master_values: pd.Series,
    fallback_part_values: pd.Series,
    *,
    after: str | None = None,
) -> None:
    master = master_values.fillna("").astype(str).str.strip()
    fallback = fallback_part_values.fillna("").astype(str).str.strip()
    master_key = master.where(master.ne(""), fallback)
    _set_or_insert_column(out, "MasterKey", master_key, after=after)


def _build_template_parts_usage_from_sap_delivery_notes(
    usage: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
    warehouse_locations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    dn = _filtered_sap_delivery_note_rows(usage)
    if dn.empty:
        return pd.DataFrame(columns=columns)

    call_number = dn.apply(_call_number_from_delivery_note, axis=1)
    delivery_note = dn["DeliveryNoteNumber"].astype(str).str.strip()
    order_number = "DN " + delivery_note
    comments = dn["Comments"]
    serial = comments.map(lambda value: _parse_labeled_value(value, r"Serial\s*(?:number|nr|no)?"))
    customer = dn.apply(_customer_name_from_delivery_note_row, axis=1)
    helpdesk = _helpdesk_by_call(issue_tracker)
    helpdesk_rows = call_number.map(lambda value: helpdesk.get(_call_match_key(value), {}))
    warehouse_code = dn["WarehouseCode"].astype(str).str.strip()
    warehouse_name = warehouse_code.str.upper().map(_warehouse_name_lookup(warehouse_locations)).fillna("")
    warehouse_name = warehouse_name.where(warehouse_name.astype(str).str.strip().ne(""), warehouse_code)
    part_code = dn["ItemNo"].map(_part_key)

    out = _blank_template(columns, len(dn))
    if "orderNumber" in out.columns:
        out["orderNumber"] = order_number
    if "requestId" in out.columns:
        out["requestId"] = call_number
    if "OrderType" in out.columns:
        out["OrderType"] = "service_order"
    if "customerCompanyCode" in out.columns:
        out["customerCompanyCode"] = customer
    if "orderStartDatetime" in out.columns:
        out["orderStartDatetime"] = helpdesk_rows.map(lambda row: _parse_any_date(row.get("Created", "")))
    if "orderStatus" in out.columns:
        out["orderStatus"] = helpdesk_rows.map(lambda row: str(row.get("Status", "") or "").strip())
    _add_parts_usage_ui_aliases(out, part_code, warehouse_code)
    if "partCode" in out.columns:
        out["partCode"] = part_code
    if "serialNumber" in out.columns:
        out["serialNumber"] = serial
    if "quantityUsed" in out.columns:
        out["quantityUsed"] = dn["QuantityNum"].abs()
    if "partsUsedDateTime" in out.columns:
        out["partsUsedDateTime"] = dn["DocDate"].map(_sap_date)
    if "Warehouse" in out.columns:
        out["Warehouse"] = warehouse_name
    if "Warehouse Code" not in out.columns:
        insert_at = out.columns.get_loc("Warehouse") + 1 if "Warehouse" in out.columns else len(out.columns)
        out.insert(insert_at, "Warehouse Code", warehouse_code)
    else:
        out["Warehouse Code"] = warehouse_code
    if "deviceSerialNumber" in out.columns:
        out["deviceSerialNumber"] = serial
    master_values = part_code.map(_master_lookup(masters)).fillna("")
    if "Master" in out.columns:
        out["Master"] = master_values
    _add_master_key_column(out, master_values, part_code, after="Master" if "Master" in out.columns else "partCode")
    return out


def _build_template_parts_usage_from_stock_audit(
    usage: pd.DataFrame,
    columns: list[str],
    masters: pd.DataFrame | None = None,
    issue_tracker: pd.DataFrame | None = None,
    warehouse_locations: pd.DataFrame | None = None,
    delivery_note_context: pd.DataFrame | None = None,
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
    dn_number = dn["Document"].astype(str).str.replace(r"^\s*DN\s+", "", regex=True).str.strip()
    context = _delivery_note_context_by_number(delivery_note_context)
    context_rows = dn_number.map(lambda value: context.get(_clean_text(value), {}))
    call_number = context_rows.map(lambda row: _call_number_from_delivery_note(pd.Series(row)) if row else "")
    order_number = dn["Document"].astype(str).str.strip()
    comments = context_rows.map(lambda row: row.get("Comments", "") if row else "")
    serial = comments.map(lambda value: _parse_labeled_value(value, r"Serial\s*(?:number|nr|no)?"))
    customer = context_rows.map(lambda row: _customer_name_from_delivery_note_row(pd.Series(row)) if row else "")
    helpdesk = _helpdesk_by_call(issue_tracker)
    helpdesk_rows = call_number.map(lambda value: helpdesk.get(_call_match_key(value), {}))
    warehouse_code = dn["Whse"].astype(str).str.strip()
    warehouse_name = warehouse_code.str.upper().map(_warehouse_name_lookup(warehouse_locations)).fillna("")
    warehouse_name = warehouse_name.where(warehouse_name.astype(str).str.strip().ne(""), warehouse_code)
    part_code = dn["Item No."].map(_part_key)

    out = _blank_template(columns, len(dn))
    if "orderNumber" in out.columns:
        out["orderNumber"] = order_number
    if "requestId" in out.columns:
        out["requestId"] = call_number
    if "OrderType" in out.columns:
        out["OrderType"] = "service_order"
    if "orderType" in out.columns:
        out["orderType"] = "service_order"
    if "customerCompanyCode" in out.columns:
        out["customerCompanyCode"] = customer
    if "orderStartDatetime" in out.columns:
        out["orderStartDatetime"] = helpdesk_rows.map(lambda row: _parse_any_date(row.get("Created", "")))
    if "orderStatus" in out.columns:
        out["orderStatus"] = helpdesk_rows.map(lambda row: str(row.get("Status", "") or "").strip())
    _add_parts_usage_ui_aliases(out, part_code, warehouse_code)
    if "partCode" in out.columns:
        out["partCode"] = part_code
    if "serialNumber" in out.columns:
        out["serialNumber"] = serial
    master_values = part_code.map(_master_lookup(masters)).fillna("")
    if "Master" in out.columns:
        out["Master"] = master_values
    if "Warehouse" in out.columns:
        out["Warehouse"] = warehouse_name
    if "Warehouse Code" not in out.columns:
        insert_at = out.columns.get_loc("Warehouse") + 1 if "Warehouse" in out.columns else len(out.columns)
        out.insert(insert_at, "Warehouse Code", warehouse_code)
    else:
        out["Warehouse Code"] = warehouse_code
    if "quantityUsed" in out.columns:
        out["quantityUsed"] = dn["QuantityNum"].abs()
    if "partsUsedDateTime" in out.columns:
        parsed = pd.to_datetime(dn["Posting Date"].astype(str).str.strip(), format="%d/%m/%y", errors="coerce")
        fallback = parsed.isna()
        if fallback.any():
            parsed.loc[fallback] = pd.to_datetime(dn.loc[fallback, "Posting Date"].astype(str).str.strip(), format="%d/%m/%Y", errors="coerce")
        out["partsUsedDateTime"] = parsed.dt.strftime("%Y-%m-%d").fillna("")
    if "deviceSerialNumber" in out.columns:
        out["deviceSerialNumber"] = serial
    _add_master_key_column(out, master_values, part_code, after="Master" if "Master" in out.columns else "partCode")
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
    warehouse_code = _col(source, "DispatchWarehouse").astype(str).str.strip()
    _add_parts_usage_ui_aliases(out, part, warehouse_code)
    if "partCode" in out.columns:
        out["partCode"] = part
    if "serialNumber" in out.columns:
        out["serialNumber"] = _col(source, "Serial Nr")
    if "quantityUsed" in out.columns:
        out["quantityUsed"] = _to_number(_col(source, "Quantity"))
    if "partsUsedDateTime" in out.columns:
        out["partsUsedDateTime"] = _col(source, "Created").map(_parse_any_date)
    if "Warehouse" in out.columns:
        out["Warehouse"] = warehouse_code
    if "Warehouse Code" not in out.columns:
        insert_at = out.columns.get_loc("Warehouse") + 1 if "Warehouse" in out.columns else len(out.columns)
        out.insert(insert_at, "Warehouse Code", warehouse_code)
    else:
        out["Warehouse Code"] = warehouse_code
    if "deviceSerialNumber" in out.columns:
        out["deviceSerialNumber"] = _col(source, "Serial Nr")
    by_part = _master_lookup(masters)
    mapped = part.map(by_part).fillna("")
    tracker_master = _col(source, "SPLMaster").astype(str).str.strip()
    master_values = tracker_master.where(tracker_master.ne(""), mapped)
    if "Master" in out.columns:
        out["Master"] = master_values
    _add_master_key_column(out, master_values, part, after="Master" if "Master" in out.columns else "partCode")
    return out.drop_duplicates(keep="first")


def build_template_service_orders(
    spares_issued: pd.DataFrame,
    columns: list[str],
    issue_tracker: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if (issue_tracker is None or issue_tracker.empty) and spares_issued.empty:
        return pd.DataFrame(columns=columns)
    spares_rows = _service_order_rows_from_spares_issued(spares_issued)
    trusted_spares_keys = set(spares_rows.get("CallMatchKey", pd.Series(dtype=str)).astype(str).str.strip())
    delivery_by_call = _spares_actual_eta_by_call(spares_issued)
    source = _combine_service_order_sources(
        _service_order_rows_from_issue_tracker(issue_tracker, trusted_spares_keys),
        spares_rows,
    )
    if source.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, object]] = []
    for match_key, group in source.groupby("CallMatchKey", sort=False):
        group = group.sort_values("CreatedDateTimeValue", na_position="last").reset_index(drop=True)
        call_number = _clean_text(group.loc[0, "ServiceCallNumber"])
        open_dt = _first_datetime(group["CreatedDateTimeValue"])
        actual_eta = delivery_by_call.get(match_key)
        if actual_eta is None and "DeliveryDateTimeValue" in group.columns:
            actual_eta = _first_datetime(group["DeliveryDateTimeValue"])
        sla = _most_common_nonblank(group.get("SLA", pd.Series(dtype=str)))
        sla_fields = _service_order_sla_fields(open_dt, sla)
        delivery_note = _first_nonblank(group.get("DeliveryNoteNumber", pd.Series(dtype=str)))
        rows.append(
            {
                "orderNumber": delivery_note or call_number,
                "RequestID": call_number,
                "location": _service_order_location(group),
                "actualEta": _format_datetime(actual_eta),
                "slaEtaClock": sla_fields["slaEtaClock"],
                "slaResolveClock": sla_fields["slaResolveClock"],
                "slaResolveDateTime": sla_fields["slaResolveDateTime"],
            }
        )

    out = _blank_template(columns, len(rows))
    source_rows = pd.DataFrame(rows)
    for column in source_rows.columns:
        if column in out.columns:
            out[column] = source_rows[column]
    return out.drop_duplicates(subset=["orderNumber"], keep="first").reset_index(drop=True) if "orderNumber" in out.columns else out


def _combine_service_order_sources(issue_rows: pd.DataFrame, spares_rows: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if not issue_rows.empty:
        issue = issue_rows.copy()
        issue["ServiceOrderSourceRank"] = 0
        frames.append(issue)
    if not spares_rows.empty:
        spares = spares_rows.copy()
        spares["ServiceOrderSourceRank"] = 1
        frames.append(spares)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    if "CallMatchKey" not in combined.columns:
        return pd.DataFrame()
    combined["CallMatchKey"] = combined["CallMatchKey"].map(_clean_text)
    combined = combined[combined["CallMatchKey"].ne("")].copy()
    if combined.empty:
        return combined
    if "ServiceOrderSourceRank" not in combined.columns:
        combined["ServiceOrderSourceRank"] = 1
    combined["ServiceOrderSourceRank"] = pd.to_numeric(combined["ServiceOrderSourceRank"], errors="coerce").fillna(1)
    return combined.sort_values("ServiceOrderSourceRank", kind="stable").reset_index(drop=True)


def _service_order_rows_from_spares_issued(spares_issued: pd.DataFrame) -> pd.DataFrame:
    if spares_issued.empty:
        return pd.DataFrame()
    source = spares_issued.copy()
    for column in ["Cust Ord No", "Order Date", "Order Time", "Del Date", "Del Time", "Del No.", "Customer ", "Customer Name"]:
        if column not in source.columns:
            source[column] = ""
    source["ServiceCallNumber"] = source["Cust Ord No"].map(_service_call_number)
    source = source[source["ServiceCallNumber"].astype(str).str.strip().ne("")].copy()
    if source.empty:
        return pd.DataFrame()
    source["CallMatchKey"] = source["ServiceCallNumber"].map(_call_match_key)
    source["OrderDateTimeValue"] = source.apply(lambda row: _report_datetime(row.get("Order Date"), row.get("Order Time")), axis=1)
    source["DeliveryDateTimeValue"] = source.apply(lambda row: _report_datetime(row.get("Del Date"), row.get("Del Time")), axis=1)
    source["CreatedDateTimeValue"] = source["OrderDateTimeValue"]
    source["SLA"] = ""
    source["DeliveryCity"] = ""
    source["CustomerNormalized"] = ""
    source["Customer"] = source["Customer "]
    source["DeliveryNoteNumber"] = source["Del No."].map(_delivery_note_id)
    return source


def _service_order_rows_from_issue_tracker(
    issue_tracker: pd.DataFrame | None,
    trusted_match_keys: set[str] | None = None,
) -> pd.DataFrame:
    if issue_tracker is None or issue_tracker.empty:
        return pd.DataFrame()
    source = issue_tracker.copy()
    for column in [
        "Call Number",
        "Subject",
        "ConversationHistory",
        "Created",
        "SLA",
        "DeliveryCity",
        "CustomerNormalized",
        "Customer",
        "Part Nr",
        "DispatchPartNo",
        "PurchaseOrder",
    ]:
        if column not in source.columns:
            source[column] = ""
    trusted_match_keys = trusted_match_keys or set()
    extracted = source.apply(lambda row: _service_request_id_from_issue_row(row, trusted_match_keys), axis=1)
    source["ServiceCallNumber"] = extracted.map(lambda item: item[0])
    source["ServiceOrderEvidence"] = extracted.map(lambda item: item[1])
    source = source[source["ServiceCallNumber"].astype(str).str.strip().ne("")].copy()
    if source.empty:
        return pd.DataFrame()
    source["CallMatchKey"] = source["ServiceCallNumber"].map(_call_match_key)
    source = source[source["CallMatchKey"].astype(str).str.strip().ne("")].copy()
    source["CreatedDateTimeValue"] = source["Created"].map(_parse_datetime_value)
    source["DeliveryNoteNumber"] = ""
    return source


def _spares_actual_eta_by_call(spares_issued: pd.DataFrame) -> dict[str, datetime]:
    source = _service_order_rows_from_spares_issued(spares_issued)
    if source.empty or "DeliveryDateTimeValue" not in source.columns:
        return {}
    lookup: dict[str, datetime] = {}
    for match_key, group in source.groupby("CallMatchKey", sort=False):
        actual_eta = _first_datetime(group["DeliveryDateTimeValue"])
        if actual_eta is not None:
            lookup[match_key] = actual_eta
    return lookup


def _service_call_number(value: object) -> str:
    text = _clean_text(value)
    if not text or text == "-":
        return ""
    if re.search(r"\b(?:PO|OPP)\b|(?:\d{2}PO|\d{2}OPP)", text, flags=re.IGNORECASE):
        return ""
    text = re.sub(r"^\s*S\s*[_-]\s*", "", text, flags=re.IGNORECASE)
    tokens = re.findall(r"\d{6,}", text)
    if len(tokens) == 1:
        return tokens[0].lstrip("0") or "0"
    if not tokens:
        return ""
    return text


def _normalise_service_request_id(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*[-/|]+\s*", " || ", text)
    text = re.sub(r"(?:\s*\|\|\s*)+", " || ", text)
    return text.strip(" .;")


def _explicit_service_request_id_from_text(value: object) -> tuple[str, str]:
    text = _clean_text(value)
    if not text:
        return "", ""
    patterns = [
        ("ticket_label", r"\bTicket\s*(?:No|Nr|Number|#)?\s*:?\s*([A-Z]{0,4}\s*\d[\d\s/|\-]{4,}\d)"),
        ("call_label", r"\bCall\s*(?:No|Nr|Number|#)?\s*:?\s*([A-Z]{0,4}\s*\d[\d\s/|\-]{4,}\d)"),
        ("service_request_label", r"\b(?:SR|Service\s*Request|Request)\s*(?:No|Nr|Number|#)?\s*:?\s*([A-Z]{0,4}\s*\d[\d\s/|\-]{4,}\d)"),
    ]
    for evidence, pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = _normalise_service_request_id(match.group(1))
            if candidate:
                return candidate, evidence
    return "", ""


def _ticket_number_from_subject(value: object) -> str:
    ticket, _ = _explicit_service_request_id_from_text(value)
    return ticket


def _looks_like_clean_service_request_id(value: object, row: pd.Series | None = None) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    upper = text.upper()
    if re.search(r"\b(?:PO|OPP)\b|(?:\d{2}PO|\d{2}OPP)|BUFFER|SHORTAGE|SPARES AVAILABILITY", upper):
        return False
    if row is not None:
        part_values = {
            _part_key(row.get("Part Nr")),
            _part_key(row.get("DispatchPartNo")),
        }
        if _part_key(text) in part_values:
            return False
    tokens = re.findall(r"\d{5,}", text)
    if len(tokens) == 1:
        return 7 <= len(tokens[0]) <= 8
    if len(tokens) == 2:
        return 7 <= len(tokens[0]) <= 8 and 5 <= len(tokens[1]) <= 8
    return False


def _service_request_id_from_issue_row(
    row: pd.Series,
    trusted_match_keys: set[str] | None = None,
) -> tuple[str, str]:
    for column in ["Subject", "ConversationHistory", "Call Number"]:
        request_id, evidence = _explicit_service_request_id_from_text(row.get(column))
        if request_id:
            return request_id, f"{evidence}:{column}"
    call_number = _clean_text(row.get("Call Number"))
    trusted_match_keys = trusted_match_keys or set()
    if _looks_like_clean_service_request_id(call_number, row) and _call_match_key(call_number) in trusted_match_keys:
        return _normalise_service_request_id(call_number), "clean_call_column:Call Number"
    return "", ""


def _service_order_location(group: pd.DataFrame) -> str:
    delivery_city = _most_common_nonblank(group.get("DeliveryCity", pd.Series(dtype=str)), excluded={"UNKNOWN", "N/A", "NA"})
    if delivery_city:
        return delivery_city
    normalized = _most_common_nonblank(group.get("CustomerNormalized", pd.Series(dtype=str)), excluded={"OTHER", "UNKNOWN", "N/A", "NA"})
    if normalized:
        return normalized
    customer = _most_common_nonblank(group.get("Customer", pd.Series(dtype=str)), excluded={"OTHER", "UNKNOWN", "N/A", "NA"})
    if customer:
        return customer
    customer_label = _most_common_text(group.get("Customer ", pd.Series(dtype=str)))
    if customer_label:
        return customer_label
    return _most_common_text(group.get("Customer Name", pd.Series(dtype=str)))


def _service_order_sla_fields(open_dt: datetime | None, sla: object) -> dict[str, str]:
    parsed = parse_customer_sla(sla)
    raw = _clean_text(sla)
    response_hours = _clean_text(parsed.get("stdResponseTime"))
    repair_hours = _clean_text(parsed.get("stdRepairTime"))
    target_hours = repair_hours or response_hours
    is_recovery = bool(re.search(r"\b(RECOVERY|RECOVER|REPAIR|RESOLVE|RESOLUTION|RECOV|REC)\b", raw.upper()))
    due_dt = _sla_target_datetime(open_dt, raw, target_hours)
    eta_clock = _format_float(_hours_between(open_dt, due_dt)) if open_dt and due_dt else target_hours
    resolve_clock = ""
    resolve_due = ""
    if is_recovery:
        resolve_clock = eta_clock
        resolve_due = _format_datetime(due_dt)
    return {
        "slaEtaClock": eta_clock,
        "slaResolveClock": resolve_clock,
        "slaResolveDateTime": resolve_due,
    }


def _sla_target_datetime(open_dt: datetime | None, raw_sla: object, hours: object) -> datetime | None:
    if open_dt is None:
        return None
    raw = _clean_text(raw_sla).upper()
    if re.search(r"\bNBD\b|\bNEXT BUSINESS DAY\b|\bNBS\b", raw):
        return datetime.combine(_next_business_day(open_dt.date()), time(17, 0))
    numeric_hours = pd.to_numeric(pd.Series([hours]), errors="coerce").iloc[0]
    if pd.isna(numeric_hours):
        return None
    return open_dt + timedelta(hours=float(numeric_hours))


def _next_business_day(start_date: date) -> date:
    candidate = start_date + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _hours_between(start: datetime | None, end: datetime | None) -> float | str:
    if start is None or end is None:
        return ""
    return max((end - start).total_seconds() / 3600, 0)


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


def build_template_part_types(altsgen_evidence: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if altsgen_evidence.empty:
        return pd.DataFrame(columns=columns)
    source = altsgen_evidence.copy()
    for column in ["status", "partType", "partTypeDescription", "isReworkable"]:
        if column not in source.columns:
            source[column] = ""
        source[column] = source[column].map(_clean_text)
    source = source[
        source["status"].str.lower().eq("ok")
        & source["partType"].str.strip().ne("")
        & source["partType"].str.lower().ne("unknown")
    ].copy()
    if source.empty:
        source = pd.DataFrame([{"partType": "unknown_component", "partTypeDescription": "Unknown Component", "isReworkable": "NO"}])
    elif "unknown_component" not in set(source["partType"].astype(str).str.strip()):
        source = pd.concat(
            [
                source,
                pd.DataFrame([{"partType": "unknown_component", "partTypeDescription": "Unknown Component", "isReworkable": "NO"}]),
            ],
            ignore_index=True,
            sort=False,
        )

    source = source.drop_duplicates(subset=["partType"], keep="last").sort_values("partType").reset_index(drop=True)
    out = _blank_template(columns, len(source))
    if "partType" in out.columns:
        out["partType"] = source["partType"]
    if "partTypeDescription" in out.columns:
        fallback = source["partType"].map(lambda value: " ".join(str(value).replace("-", "_").split("_")).title())
        out["partTypeDescription"] = source["partTypeDescription"].where(source["partTypeDescription"].str.strip().ne(""), fallback)
    if "isReworkable" in out.columns:
        out["isReworkable"] = "NO"
    return out[columns].reset_index(drop=True)


def build_parts_usage_evidence(
    usage: pd.DataFrame,
    masters: pd.DataFrame | None,
    issue_tracker: pd.DataFrame | None = None,
    warehouse_locations: pd.DataFrame | None = None,
    delivery_note_context: pd.DataFrame | None = None,
) -> pd.DataFrame:
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
        issue_tracker,
        warehouse_locations,
        delivery_note_context,
    )
    if template.empty:
        return pd.DataFrame(columns=columns)
    by_part = _master_lookup(masters)
    if "DeliveryNoteNumber" in usage.columns:
        source_rows = _filtered_sap_delivery_note_rows(usage)
        delivery_notes = _col(source_rows, "DeliveryNoteNumber").astype(str).str.strip()
    else:
        work = usage.copy()
        for column in ["Item No.", "Description"]:
            if column in work.columns:
                work[column] = work[column].replace(r"^\s*$", pd.NA, regex=True).ffill().fillna("")
        for column in ["Document", "Posting Date", "Quantity"]:
            if column not in work.columns:
                work[column] = ""
        work["QuantityNum"] = _to_number(work["Quantity"])
        source_rows = work[
            work["Document"].astype(str).str.startswith("DN ", na=False)
            & work["Posting Date"].astype(str).str.strip().ne("")
            & work["Item No."].astype(str).str.strip().ne("")
            & work["QuantityNum"].lt(0)
        ].copy().drop_duplicates(keep="first").reset_index(drop=True)
        delivery_notes = source_rows["Document"].astype(str).str.replace(r"^\s*DN\s+", "", regex=True).str.strip()
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
    return out.drop_duplicates(keep="first").reset_index(drop=True)


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


def build_customer_evidence(usage: pd.DataFrame, issue_tracker: pd.DataFrame | None) -> pd.DataFrame:
    columns = [
        "SapParsedCustomer",
        "SapCardCode",
        "SapCardName",
        "DeliveryNoteRows",
        "BestHelpDeskCustomerMatch",
        "HelpDeskRows",
        "MatchType",
        "CustomerIdDecision",
        "StdResponseTime",
        "StdRepairTime",
        "SlaEvidence",
    ]
    sap_rows = _customer_rows_from_sap_delivery_notes(usage)
    helpdesk_rows = _customer_rows_from_helpdesk(issue_tracker)
    if sap_rows.empty and helpdesk_rows.empty:
        return pd.DataFrame(columns=columns)

    sap_card = pd.DataFrame()
    if usage is not None and not usage.empty:
        sap_card = usage.copy()
        for column in ["CardCode", "CardName", "Comments"]:
            if column not in sap_card.columns:
                sap_card[column] = ""
        sap_card["CustomerName"] = sap_card["Comments"].map(lambda value: _clean_customer_name(_parse_labeled_value(value, r"Customer")))
        sap_card["CustomerKey"] = sap_card["CustomerName"].map(_customer_match_key)
        sap_card = sap_card[sap_card["CustomerKey"].astype(str).str.strip().ne("")]
        if not sap_card.empty:
            sap_card = (
                sap_card.groupby("CustomerKey", dropna=False)
                .agg(
                    SapCardCode=("CardCode", _most_common_text),
                    SapCardName=("CardName", _most_common_text),
                )
                .reset_index()
            )

    helpdesk = helpdesk_rows.copy()
    if not helpdesk.empty:
        helpdesk["CustomerKey"] = helpdesk["CustomerName"].map(_customer_match_key)
    rows: list[dict[str, object]] = []
    all_keys = sorted(
        set(sap_rows["CustomerName"].map(_customer_match_key) if not sap_rows.empty else [])
        | set(helpdesk["CustomerKey"] if not helpdesk.empty else [])
    )
    sap_by_key = sap_rows.assign(CustomerKey=sap_rows["CustomerName"].map(_customer_match_key)).set_index("CustomerKey") if not sap_rows.empty else pd.DataFrame()
    hd_by_key = helpdesk.set_index("CustomerKey") if not helpdesk.empty else pd.DataFrame()
    card_by_key = sap_card.set_index("CustomerKey") if not sap_card.empty else pd.DataFrame()
    for key in all_keys:
        sap_match = sap_by_key.loc[key] if key in sap_by_key.index else None
        hd_match = hd_by_key.loc[key] if key in hd_by_key.index else None
        card_match = card_by_key.loc[key] if key in card_by_key.index else None
        sap_customer = _clean_text(sap_match.get("CustomerName")) if sap_match is not None else ""
        hd_customer = _clean_text(hd_match.get("CustomerName")) if hd_match is not None else ""
        if sap_customer and hd_customer:
            match_type = "Exact normalized SAP DN Customer label + HelpDesk Customer"
        elif sap_customer:
            match_type = "SAP DN Customer label only"
        else:
            match_type = "HelpDesk Customer only"
        hd_response = _clean_text(hd_match.get("StdResponseTime")) if hd_match is not None else ""
        hd_repair = _clean_text(hd_match.get("StdRepairTime")) if hd_match is not None else ""
        hd_sla = _clean_text(hd_match.get("SlaEvidence")) if hd_match is not None else ""
        sap_response = _clean_text(sap_match.get("StdResponseTime")) if sap_match is not None else ""
        sap_repair = _clean_text(sap_match.get("StdRepairTime")) if sap_match is not None else ""
        sap_sla = _clean_text(sap_match.get("SlaEvidence")) if sap_match is not None else ""
        response = hd_response
        repair = hd_repair
        sla = hd_sla
        if not (response or repair) and (sap_response or sap_repair):
            response = sap_response
            repair = sap_repair
            sla = sap_sla
        elif not response and sap_match is not None:
            response = _clean_text(sap_match.get("StdResponseTime"))
        elif not repair and sap_match is not None:
            repair = _clean_text(sap_match.get("StdRepairTime"))
        if not sla:
            sla = sap_sla
        rows.append(
            {
                "SapParsedCustomer": sap_customer,
                "SapCardCode": _clean_text(card_match.get("SapCardCode")) if card_match is not None else "",
                "SapCardName": _clean_text(card_match.get("SapCardName")) if card_match is not None else "",
                "DeliveryNoteRows": _clean_text(sap_match.get("DeliveryNoteRows")) if sap_match is not None else "",
                "BestHelpDeskCustomerMatch": hd_customer,
                "HelpDeskRows": _clean_text(hd_match.get("TicketRows")) if hd_match is not None else "",
                "MatchType": match_type,
                "CustomerIdDecision": "No downstream SAP customer ID proven; SAP CardCode is CoCre8 BP, not the end customer.",
                "StdResponseTime": response,
                "StdRepairTime": repair,
                "SlaEvidence": sla,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def write_review_evidence(
    evidence_dir: Path,
    usage: pd.DataFrame,
    purchase_orders: pd.DataFrame,
    issue_tracker: pd.DataFrame,
    masters: pd.DataFrame | None,
    manual_warehouses: pd.DataFrame | None = None,
    warehouses: pd.DataFrame | None = None,
    parts: pd.DataFrame | None = None,
    spi_costs: pd.DataFrame | None = None,
    parts_usage_source: pd.DataFrame | None = None,
    delivery_note_context: pd.DataFrame | None = None,
    warehouse_locations: pd.DataFrame | None = None,
) -> list[Path]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    usage_for_evidence = parts_usage_source if parts_usage_source is not None and not parts_usage_source.empty else usage
    files = [
        (
            build_parts_usage_evidence(
                usage_for_evidence,
                masters,
                issue_tracker,
                warehouse_locations,
                delivery_note_context,
            ),
            evidence_dir / "PartsUsage_SPLMaster_Evidence.csv",
        ),
        (build_purchase_order_evidence(purchase_orders, masters, issue_tracker), evidence_dir / "PurchaseOrders_SPLMaster_Evidence.csv"),
        (purchase_order_reconciliation(purchase_orders, issue_tracker, masters), evidence_dir / "PurchaseOrders_Ticket_Reconciliation.csv"),
        (issue_tracker_evidence_rows(issue_tracker, masters), evidence_dir / "IssueTracker_Line_Evidence.csv"),
        (build_customer_evidence(usage, issue_tracker), evidence_dir / "Customers_SAP_HelpDesk_Evidence.csv"),
        (
            _part_cost_rows(
                parts if parts is not None else pd.DataFrame(),
                purchase_orders,
                spi_costs if spi_costs is not None else pd.DataFrame(),
                masters,
            ),
            evidence_dir / "PartCost_Evidence.csv",
        ),
        (
            build_warehouse_exclusion_evidence(
                warehouses if warehouses is not None else pd.DataFrame(),
                manual_warehouses,
            ),
            evidence_dir / "WarehouseExclusions_Evidence.csv",
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
    warehouse_ids = out["warehouseId"].astype(str).str.strip() if "warehouseId" in out.columns else pd.Series([""] * len(out))
    if "isPrimary" not in out.columns:
        insert_at = out.columns.get_loc("warehouseDescription") + 1 if "warehouseDescription" in out.columns else len(out.columns)
        out.insert(insert_at, "isPrimary", "")
    out["isPrimary"] = warehouse_ids.str.upper().eq("FUJITSU").map({True: "Y", False: "N"})
    if manual_warehouses is not None and not manual_warehouses.empty and "warehouseId" in manual_warehouses.columns:
        manual = manual_warehouses.copy()
        manual["warehouseId"] = manual["warehouseId"].astype(str).str.strip()
        manual_by_id = manual.drop_duplicates(subset=["warehouseId"], keep="first").set_index("warehouseId")
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
    if "addressId" in out.columns:
        out["addressId"] = warehouse_ids.map(_warehouse_address_id)
    return out.drop_duplicates(subset=["warehouseId"], keep="first") if "warehouseId" in out.columns else out


def build_template_warehouse_exclusions(
    warehouses: pd.DataFrame,
    columns: list[str],
    manual_warehouses: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build route-level stock-pooling exclusions between CoCre8 warehouse pools."""

    warehouse_ids = _warehouse_exclusion_ids(warehouses, manual_warehouses)
    rows: list[dict[str, str]] = []
    for to_warehouse in warehouse_ids:
        to_pool = _warehouse_interchangeability_pool(to_warehouse)
        for from_warehouse in warehouse_ids:
            if to_warehouse == from_warehouse:
                continue
            if to_pool == _warehouse_interchangeability_pool(from_warehouse):
                continue
            rows.append(
                {
                    "Secondary TO": to_warehouse,
                    "Secondary FROM": from_warehouse,
                    "Cross TO": to_warehouse,
                    "Cross FROM": from_warehouse,
                    "Primary TO": "",
                }
            )
    if not rows:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(rows)
    for column in columns:
        if column not in out.columns:
            out[column] = ""
    return out[columns].drop_duplicates().reset_index(drop=True)


def build_warehouse_exclusion_evidence(
    warehouses: pd.DataFrame,
    manual_warehouses: pd.DataFrame | None = None,
) -> pd.DataFrame:
    warehouse_ids = _warehouse_exclusion_ids(warehouses, manual_warehouses)
    pool_members = {
        pool_id: sorted(members)
        for pool_id, members in WAREHOUSE_INTERCHANGEABILITY_POOLS.items()
        if any(member in warehouse_ids for member in members)
    }
    rows = []
    for warehouse_id in warehouse_ids:
        pool = _warehouse_interchangeability_pool(warehouse_id)
        rows.append(
            {
                "warehouseId": warehouse_id,
                "interchangeabilityPool": pool,
                "poolMembersPresent": ", ".join(pool_members.get(pool, [warehouse_id])),
                "decision": "Can share stock only with warehouses in the same pool.",
            }
        )
    return pd.DataFrame(
        rows,
        columns=["warehouseId", "interchangeabilityPool", "poolMembersPresent", "decision"],
    )


def _warehouse_exclusion_ids(
    warehouses: pd.DataFrame,
    manual_warehouses: pd.DataFrame | None = None,
) -> list[str]:
    frames: list[pd.Series] = []
    if not warehouses.empty:
        frames.append(_first_col(warehouses, ["WarehouseCode", "warehouseId", "warehouseCode"]).map(_clean_text))
    if manual_warehouses is not None and not manual_warehouses.empty:
        frames.append(_first_col(manual_warehouses, ["warehouseId", "WarehouseCode", "warehouseCode"]).map(_clean_text))
    if not frames:
        return []
    ids = pd.concat(frames, ignore_index=True).astype(str).str.strip()
    ids = ids[ids.ne("")]
    if ids.empty:
        return []
    work = pd.DataFrame({"warehouseId": ids})
    work["warehouseKey"] = work["warehouseId"].str.upper()
    work = work[~_excluded_warehouse_mask(work["warehouseId"])]
    work = work.drop_duplicates(subset=["warehouseKey"], keep="first")
    return sorted(work["warehouseId"].tolist(), key=lambda value: value.upper())


def _warehouse_interchangeability_pool(warehouse_id: object) -> str:
    key = _clean_text(warehouse_id).upper()
    for pool_id, members in WAREHOUSE_INTERCHANGEABILITY_POOLS.items():
        if key in {member.upper() for member in members}:
            return pool_id
    return f"warehouse:{key}"


def _warehouse_address_id(warehouse_id: object) -> str:
    key = _clean_text(warehouse_id).upper()
    if key in {warehouse.upper() for warehouse in FUJ_CT_ADDRESS_WAREHOUSES}:
        return "FUJ CT"
    return "FUJITSU"


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
    part_number = _col(inventory, "ItemNo")
    if "Ws Part" in out.columns:
        out["Ws Part"] = part_number
    elif "partCode" in out.columns:
        out.insert(out.columns.get_loc("partCode"), "Ws Part", part_number)
    else:
        out["Ws Part"] = part_number
    if "partCode" in out.columns:
        out["partCode"] = part_number
    if "partNumber" in out.columns:
        out["partNumber"] = part_number
    if "warehouseCode" in out.columns:
        out["warehouseCode"] = _col(inventory, "WarehouseCode")
    if "quantityAllocated" in out.columns:
        out["quantityAllocated"] = _to_number(_first_col(inventory, ["IsCommited", "Commited"]))
    if "quantityOnHand" in out.columns:
        out["quantityOnHand"] = _to_number(_first_col(inventory, ["OnHand", "Quantity"]))
    if "quantityInbound" in out.columns:
        out["quantityInbound"] = _to_number(_first_col(inventory, ["OnOrder", "Ordered"]))
    if "quantityOutbound" in out.columns:
        out["quantityOutbound"] = 0
    if "uniqueId" in out.columns:
        by_part = _master_lookup(masters)
        part_keys = part_number.map(_part_key)
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
    vendor_name = _first_col(source, ["VendorName", "vendorName", "Description", "CardName"])
    if "Description" in out.columns:
        out["Description"] = vendor_name
    if "vendorName" in out.columns:
        out["vendorName"] = vendor_name
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
                    "movementType": "Internal_move",
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


def build_template_customers(
    customers: pd.DataFrame,
    columns: list[str],
    issue_tracker: pd.DataFrame | None = None,
    usage: pd.DataFrame | None = None,
) -> pd.DataFrame:
    source = _combined_customer_rows(issue_tracker, usage)
    if source.empty and not customers.empty:
        source = customers[_col(customers, "CustomerCode").astype(str).str.strip().ne("")].copy()
    if source.empty:
        return pd.DataFrame(columns=columns)

    out = _blank_template(columns, len(source))
    if "customerId" in out.columns:
        customer_code = _col(source, "CustomerCode")
        out["customerId"] = customer_code.where(
            customer_code.astype(str).str.strip().ne(""),
            _col(source, "CustomerName").map(_generated_customer_id),
        )
    if "Description" in out.columns:
        out["Description"] = _col(source, "CustomerName")
    if "customerName" in out.columns:
        out["customerName"] = _col(source, "CustomerName")
    if "assignAnySkill" in out.columns:
        out["assignAnySkill"] = "Y"
    if "isActive" in out.columns:
        out["isActive"] = "Y"
    if "stdResponseTime" in out.columns:
        out["stdResponseTime"] = _col(source, "StdResponseTime")
    if "stdRepairTime" in out.columns:
        out["stdRepairTime"] = _col(source, "StdRepairTime")

    subset = ["customerId"] if "customerId" in out.columns and out["customerId"].astype(str).str.strip().ne("").any() else ["customerName"]
    return out.drop_duplicates(subset=subset, keep="first").reset_index(drop=True)


def build_template_addresses(
    usage: pd.DataFrame,
    columns: list[str],
    warehouse_locations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    source = pd.concat(
        [
            _address_rows_from_sap_delivery_notes(usage, warehouse_locations),
            _warehouse_address_rows(),
        ],
        ignore_index=True,
        sort=False,
    ).fillna("")
    if source.empty:
        return pd.DataFrame(columns=columns)
    out = _blank_template(columns, len(source))
    for column in [
        "externalAddressId",
        "customerExternalId",
        "addressLine1",
        "addressLine2",
        "addressLine3",
        "city",
        "stateProvince",
        "countryCode",
        "latitude",
        "longitude",
        "timeZone",
    ]:
        if column in out.columns:
            out[column] = _col(source, column)
    if "nodeId" in out.columns:
        out["nodeId"] = ""
    if "externalAddressId" in out.columns:
        return out.drop_duplicates(subset=["externalAddressId"], keep="first").reset_index(drop=True)
    return out.drop_duplicates(keep="first").reset_index(drop=True)


def _warehouse_address_rows() -> pd.DataFrame:
    columns = [
        "externalAddressId",
        "customerExternalId",
        "CustomerName",
        "RawShipToAddress",
        "addressLine1",
        "addressLine2",
        "addressLine3",
        "city",
        "stateProvince",
        "countryCode",
        "latitude",
        "longitude",
        "timeZone",
        "WarehouseCode",
        "LocationEvidence",
    ]
    return pd.DataFrame(WAREHOUSE_ADDRESS_ROWS, columns=columns)


def _address_rows_from_sap_delivery_notes(
    usage: pd.DataFrame,
    warehouse_locations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    columns = [
        "externalAddressId",
        "customerExternalId",
        "CustomerName",
        "RawShipToAddress",
        "addressLine1",
        "addressLine2",
        "addressLine3",
        "city",
        "stateProvince",
        "countryCode",
        "latitude",
        "longitude",
        "timeZone",
        "WarehouseCode",
        "LocationEvidence",
    ]
    if usage.empty or "ShipToAddress" not in usage.columns:
        return pd.DataFrame(columns=columns)
    work = usage.copy()
    for column in ["ShipToAddress", "Comments", "CustomerRefNumber", "ShipToCode", "WarehouseCode"]:
        if column not in work.columns:
            work[column] = ""
    work["CustomerName"] = work.apply(_customer_name_from_delivery_note_row, axis=1)
    work["CustomerExternalId"] = work["CustomerName"].map(_generated_customer_id)
    work["RawShipToAddress"] = work["ShipToAddress"].map(_clean_address_text)
    work = work[
        work["CustomerExternalId"].astype(str).str.strip().ne("")
        & work["RawShipToAddress"].astype(str).str.strip().ne("")
    ].copy()
    if work.empty:
        return pd.DataFrame(columns=columns)

    work["AddressKey"] = work.apply(
        lambda row: _generated_address_id(row["CustomerExternalId"], row["RawShipToAddress"]),
        axis=1,
    )
    location_by_warehouse = _warehouse_location_lookup(warehouse_locations)
    rows: list[dict[str, object]] = []
    for address_id, group in work.groupby("AddressKey", sort=False):
        row = group.iloc[0]
        raw_address = _most_common_text(group["RawShipToAddress"])
        customer_name = _most_common_text(group["CustomerName"])
        customer_id = _generated_customer_id(customer_name)
        address_line1, address_line2, address_line3 = _address_lines(raw_address)
        location = _infer_address_location(raw_address, row.get("WarehouseCode"), customer_name, location_by_warehouse)
        rows.append(
            {
                "externalAddressId": address_id,
                "customerExternalId": customer_id,
                "CustomerName": customer_name,
                "RawShipToAddress": raw_address,
                "addressLine1": address_line1,
                "addressLine2": address_line2,
                "addressLine3": address_line3,
                "city": location["city"],
                "stateProvince": location["stateProvince"],
                "countryCode": location["countryCode"],
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "timeZone": "South Africa Standard Time",
                "WarehouseCode": _clean_text(row.get("WarehouseCode")),
                "LocationEvidence": location["evidence"],
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(["CustomerName", "externalAddressId"]).reset_index(drop=True)


def _customer_name_from_delivery_note_row(row: pd.Series) -> str:
    customer = _clean_customer_name(_parse_labeled_value(row.get("Comments"), r"Customer"))
    if customer:
        return customer
    reference = _clean_text(row.get("CustomerRefNumber"))
    match = re.match(r"([A-Za-z][A-Za-z0-9 &.-]{1,30})\s*[:_]", reference)
    if match:
        candidate = _clean_customer_name(match.group(1))
        if candidate and candidate.upper() not in {"S", "W", "WO", "PO", "CALL"}:
            return candidate
    return ""


def _clean_address_text(value: object) -> str:
    text = _clean_text(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean_extracted_value(line) for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _address_lines(address: object) -> tuple[str, str, str]:
    lines = [_clean_extracted_value(line) for line in _clean_address_text(address).split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return "", "", ""
    line1 = lines[0]
    line2 = lines[1] if len(lines) > 1 else ""
    line3 = " | ".join(lines[2:])
    return line1[:255], line2[:255], line3[:255]


def _generated_address_id(customer_id: object, address: object) -> str:
    key = f"{_clean_text(customer_id)}|{_normalised_address_key(address)}"
    return f"CC8ADDR-{zlib.crc32(key.encode('utf-8')) & 0xFFFFFFFF:08X}"


def _normalised_address_key(value: object) -> str:
    text = _clean_address_text(value).upper()
    text = re.sub(r"\bSOUTH AFRICA\b", "", text)
    text = re.sub(r"\bCONTACT(?: PERSON)?\b.*", "", text)
    text = re.sub(r"\bATT(?:ENTION)?\b.*", "", text)
    text = re.sub(r"\bCELL\b.*", "", text)
    text = re.sub(r"\bTEL(?:EPHONE)?\b.*", "", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _warehouse_location_lookup(warehouse_locations: pd.DataFrame | None) -> dict[str, dict[str, str]]:
    if warehouse_locations is None or warehouse_locations.empty:
        return {}
    lookup: dict[str, dict[str, str]] = {}
    for _, row in warehouse_locations.iterrows():
        code = _clean_text(row.get("WarehouseCode")).upper()
        if not code:
            continue
        lookup.setdefault(
            code,
            {
                "Location": _clean_text(row.get("Location")).upper(),
                "WarehouseName": _clean_text(row.get("WarehouseName")),
            },
        )
    return lookup


CITY_LOCATION_RULES = [
    ("MBABANE", "Mbabane", "Hhohho", "SZ", "-26.3054", "31.1367"),
    ("SIMUNYE", "Simunye", "Lubombo", "SZ", "-26.2070", "31.9270"),
    ("SWAZI", "Mbabane", "Hhohho", "SZ", "-26.3054", "31.1367"),
    ("ESWATINI", "Mbabane", "Hhohho", "SZ", "-26.3054", "31.1367"),
    ("CAPE TOWN", "Cape Town", "Western Cape", "ZA", "-33.9249", "18.4241"),
    ("MONTAGUE GARDENS", "Cape Town", "Western Cape", "ZA", "-33.8616", "18.5204"),
    ("CPT", "Cape Town", "Western Cape", "ZA", "-33.9249", "18.4241"),
    ("JOHANNESBURG", "Johannesburg", "Gauteng", "ZA", "-26.2041", "28.0473"),
    ("MIDRAND", "Johannesburg", "Gauteng", "ZA", "-26.0167", "28.1250"),
    ("JHB", "Johannesburg", "Gauteng", "ZA", "-26.2041", "28.0473"),
    ("PRETORIA", "Pretoria", "Gauteng", "ZA", "-25.7479", "28.2293"),
    ("DURBAN", "Durban", "KwaZulu-Natal", "ZA", "-29.8587", "31.0218"),
    ("KZN", "Durban", "KwaZulu-Natal", "ZA", "-29.8587", "31.0218"),
    ("EAST LONDON", "East London", "Eastern Cape", "ZA", "-33.0292", "27.8546"),
    ("PORT ELIZABETH", "Port Elizabeth", "Eastern Cape", "ZA", "-33.9608", "25.6022"),
    ("GQEBERHA", "Port Elizabeth", "Eastern Cape", "ZA", "-33.9608", "25.6022"),
    ("MTHATHA", "Mthatha", "Eastern Cape", "ZA", "-31.5889", "28.7844"),
    ("ZWELITSHA", "Zwelitsha", "Eastern Cape", "ZA", "-32.9000", "27.4333"),
    ("EC", "East London", "Eastern Cape", "ZA", "-33.0292", "27.8546"),
    ("BLOEMFONTEIN", "Bloemfontein", "Free State", "ZA", "-29.0852", "26.1596"),
    ("NELSPRUIT", "Nelspruit", "Mpumalanga", "ZA", "-25.4753", "30.9694"),
    ("MBOMBELA", "Nelspruit", "Mpumalanga", "ZA", "-25.4753", "30.9694"),
    ("MIDDELBURG", "Middelburg", "Mpumalanga", "ZA", "-25.7751", "29.4648"),
    ("POLOKWANE", "Polokwane", "Limpopo", "ZA", "-23.9045", "29.4689"),
    ("POTCHEFSTROOM", "Potchefstroom", "North West", "ZA", "-26.7153", "27.0970"),
    ("GEORGE", "George", "Western Cape", "ZA", "-33.9648", "22.4617"),
    ("MOSSELBAY", "Mossel Bay", "Western Cape", "ZA", "-34.1831", "22.1460"),
    ("MOSSEL BAY", "Mossel Bay", "Western Cape", "ZA", "-34.1831", "22.1460"),
    ("NORTHERN CAPE", "Kimberley", "Northern Cape", "ZA", "-28.7282", "24.7499"),
    ("NC", "Kimberley", "Northern Cape", "ZA", "-28.7282", "24.7499"),
]


LOCATION_CODE_RULES = {
    "JHB": ("Johannesburg", "Gauteng", "ZA", "-26.2041", "28.0473"),
    "CPT": ("Cape Town", "Western Cape", "ZA", "-33.9249", "18.4241"),
    "KZN": ("Durban", "KwaZulu-Natal", "ZA", "-29.8587", "31.0218"),
    "EC": ("East London", "Eastern Cape", "ZA", "-33.0292", "27.8546"),
    "NC": ("Kimberley", "Northern Cape", "ZA", "-28.7282", "24.7499"),
    "SWAZI": ("Mbabane", "Hhohho", "SZ", "-26.3054", "31.1367"),
    "TBI": ("Johannesburg", "Gauteng", "ZA", "-26.2041", "28.0473"),
}


def _infer_address_location(
    address: object,
    warehouse_code: object,
    customer_name: object,
    warehouse_lookup: dict[str, dict[str, str]],
) -> dict[str, str]:
    address_text = _clean_address_text(address)
    combined = f"{address_text}\n{_clean_text(customer_name)}\n{_clean_text(warehouse_code)}".upper()
    warehouse_key = _clean_text(warehouse_code).upper()
    warehouse = warehouse_lookup.get(warehouse_key, {})

    if re.search(r"\b(RSSC|ROYAL ESWATINI|ROYAL SWAZI|SWAZI|ESWATINI)\b", combined):
        city, state, country, lat, lon = LOCATION_CODE_RULES["SWAZI"]
        if "SIMUNYE" in combined:
            city, state, country, lat, lon = "Simunye", "Lubombo", "SZ", "-26.2070", "31.9270"
        return _location_result(city, state, country, lat, lon, "Swazi/RSSC/Eswatini keyword")

    if _is_collection_address(address_text) and warehouse:
        return _location_from_code(warehouse.get("Location"), f"collection warehouse {warehouse_key}")

    if "SANLAM" in combined:
        return _location_from_code("CPT", "Sanlam default to Cape Town")

    if "MASSMART" in combined:
        if re.search(r"\b(CPT|CAPE TOWN|FUJMSM C)\b", combined):
            return _location_from_code("CPT", "Massmart Cape Town keyword")
        return _location_from_code("JHB", "Massmart default to Johannesburg")

    for token, city, state, country, lat, lon in CITY_LOCATION_RULES:
        if re.search(rf"\b{re.escape(token)}\b", combined):
            return _location_result(city, state, country, lat, lon, f"address keyword {token}")

    if warehouse:
        return _location_from_code(warehouse.get("Location"), f"warehouse {warehouse_key}")

    return _location_result("Johannesburg", "Gauteng", "ZA", "-26.2041", "28.0473", "default ZA/Johannesburg")


def _is_collection_address(address: object) -> bool:
    text = _clean_text(address).upper()
    return bool(re.search(r"\b(COLLECT|COLLECTION|TO COLLECT|KELVIN)\b", text))


def _location_from_code(location_code: object, evidence: str) -> dict[str, str]:
    code = _clean_text(location_code).upper()
    city, state, country, lat, lon = LOCATION_CODE_RULES.get(code, LOCATION_CODE_RULES["JHB"])
    return _location_result(city, state, country, lat, lon, evidence)


def _location_result(city: str, state: str, country: str, latitude: str, longitude: str, evidence: str) -> dict[str, str]:
    return {
        "city": city,
        "stateProvince": state,
        "countryCode": country,
        "latitude": latitude,
        "longitude": longitude,
        "evidence": evidence,
    }


def _combined_customer_rows(issue_tracker: pd.DataFrame | None, usage: pd.DataFrame | None = None) -> pd.DataFrame:
    helpdesk = _customer_rows_from_helpdesk(issue_tracker)
    sap = _customer_rows_from_sap_delivery_notes(usage)
    frames = [frame for frame in [helpdesk, sap] if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["CustomerCode", "CustomerName", "TicketRows", "StdResponseTime", "StdRepairTime"])
    combined = pd.concat(frames, ignore_index=True, sort=False).fillna("")
    combined["CustomerKey"] = combined["CustomerName"].map(_customer_match_key)
    combined = combined[combined["CustomerKey"].astype(str).str.strip().ne("")]
    if combined.empty:
        return pd.DataFrame(columns=["CustomerCode", "CustomerName", "TicketRows", "StdResponseTime", "StdRepairTime"])

    rows: list[dict[str, object]] = []
    for _, group in combined.groupby("CustomerKey", sort=False):
        response, repair, evidence = _select_combined_sla(group)
        rows.append(
            {
                "CustomerCode": "",
                "CustomerName": _most_common_text(group["CustomerName"]),
                "TicketRows": int(_to_number(_col(group, "TicketRows")).sum()),
                "DeliveryNoteRows": int(_to_number(_col(group, "DeliveryNoteRows")).sum()),
                "StdResponseTime": response,
                "StdRepairTime": repair,
                "SlaEvidence": evidence,
            }
        )
    return pd.DataFrame(rows).sort_values(["CustomerName"], key=lambda col: col.str.lower()).reset_index(drop=True)


def _customer_rows_from_helpdesk(issue_tracker: pd.DataFrame | None) -> pd.DataFrame:
    if issue_tracker is None or issue_tracker.empty or "Customer" not in issue_tracker.columns:
        return pd.DataFrame(columns=["CustomerCode", "CustomerName", "TicketRows", "StdResponseTime", "StdRepairTime"])
    work = issue_tracker.copy()
    work["CustomerName"] = work["Customer"].map(_clean_text)
    work = work[work["CustomerName"].str.strip().ne("")].copy()
    if work.empty:
        return pd.DataFrame(columns=["CustomerCode", "CustomerName", "TicketRows", "StdResponseTime", "StdRepairTime"])

    work["CustomerKey"] = work["CustomerName"].map(_customer_match_key)
    rows: list[dict[str, object]] = []
    for _, group in work.groupby("CustomerKey", dropna=False):
        response, repair, evidence = _sla_summary_from_values(_col(group, "SLA"))
        rows.append(
            {
                "CustomerName": _most_common_text(group["CustomerName"]),
                "TicketRows": len(group),
                "StdResponseTime": response,
                "StdRepairTime": repair,
                "SlaEvidence": evidence,
            }
        )
    grouped = pd.DataFrame(rows).sort_values(["CustomerName"], key=lambda col: col.str.lower())
    grouped["CustomerCode"] = ""
    return grouped[["CustomerCode", "CustomerName", "TicketRows", "StdResponseTime", "StdRepairTime", "SlaEvidence"]].reset_index(drop=True)


def _customer_rows_from_sap_delivery_notes(usage: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["CustomerCode", "CustomerName", "DeliveryNoteRows", "StdResponseTime", "StdRepairTime", "SlaEvidence"]
    if usage is None or usage.empty or "Comments" not in usage.columns:
        return pd.DataFrame(columns=columns)
    work = usage.copy()
    work["CustomerName"] = work["Comments"].map(lambda value: _clean_customer_name(_parse_labeled_value(value, r"Customer")))
    work = work[work["CustomerName"].astype(str).str.strip().ne("")].copy()
    if work.empty:
        return pd.DataFrame(columns=columns)
    work["CustomerKey"] = work["CustomerName"].map(_customer_match_key)
    work = work[work["CustomerKey"].astype(str).str.strip().ne("")]
    work["SlaRaw"] = work["Comments"].map(lambda value: _parse_labeled_value(value, r"SLA"))
    rows: list[dict[str, object]] = []
    for _, group in work.groupby("CustomerKey", dropna=False):
        response, repair, evidence = _sla_summary_from_values(group["SlaRaw"])
        rows.append(
            {
                "CustomerName": _most_common_text(group["CustomerName"]),
                "DeliveryNoteRows": len(group),
                "StdResponseTime": response,
                "StdRepairTime": repair,
                "SlaEvidence": evidence,
            }
        )
    grouped = pd.DataFrame(rows).sort_values(["CustomerName"], key=lambda col: col.str.lower())
    grouped["CustomerCode"] = ""
    return grouped[columns].reset_index(drop=True)


def _most_common_text(values: pd.Series) -> str:
    cleaned = values.map(_clean_text)
    cleaned = cleaned[cleaned.str.strip().ne("")]
    if cleaned.empty:
        return ""
    return str(cleaned.value_counts().idxmax())


def _most_common_nonblank(values: pd.Series, excluded: set[str] | None = None) -> str:
    cleaned = values.map(_clean_text)
    cleaned = cleaned[cleaned.str.strip().ne("")]
    if excluded:
        cleaned = cleaned[~cleaned.str.upper().isin({value.upper() for value in excluded})]
    if cleaned.empty:
        return ""
    return str(cleaned.value_counts().idxmax())


def _first_nonblank(values: pd.Series) -> str:
    cleaned = values.map(_clean_text)
    cleaned = cleaned[cleaned.str.strip().ne("")]
    if cleaned.empty:
        return ""
    return str(cleaned.iloc[0])


def _delivery_note_id(value: object) -> str:
    text = _clean_text(value)
    if not text or text == "-":
        return ""
    text = re.sub(r"\.0$", "", text)
    if text.upper().startswith("DN "):
        return text
    return f"DN {text}"


def _customer_match_key(value: object) -> str:
    text = _clean_text(value).upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    aliases = {
        "WCED": "WESTERN CAPE EDUCATION DEPARTMENT",
        "IEC": "INDEPENDENT ELECTORAL COMMISSION",
        "DALRRD": "DEPARTMENT OF AGRICULTURE LAND REFORM AND RURAL DEVELOPMENT",
        "DEDEAT": "DEPARTMENT OF ECONOMIC DEVELOPMENT ENVIRONMENTAL AFFAIRS AND TOURISM",
        "E C DOE": "EASTERN CAPE DOE",
        "EC DOE": "EASTERN CAPE DOE",
        "CO CRE8": "COCRE8",
    }
    text = re.sub(r"\s+", " ", text).strip()
    return aliases.get(text, text)


def _clean_customer_name(value: object) -> str:
    text = _clean_extracted_value(value)
    text = re.sub(r"\bBased On Sales.*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\bBased$", "", text, flags=re.IGNORECASE).strip()
    text = text.strip(" :;,-")
    if not text:
        return ""
    upper = text.upper()
    blocked = {
        "N/A",
        "NA",
        "BASED",
        "BASED ON SALES",
        "BASED ON SALES ORDER",
        "BASED ON SALES ORDERS",
        "BOOK TO COCRE8",
    }
    if upper in blocked or upper.startswith("BASED ON SALES"):
        return ""
    return text


def _generated_customer_id(value: object) -> str:
    key = _customer_match_key(value)
    if not key:
        return ""
    return f"CC8CUST-{zlib.crc32(key.encode('utf-8')) & 0xFFFFFFFF:08X}"


def _sla_summary_from_values(values: pd.Series) -> tuple[str, str, str]:
    parsed = [parse_customer_sla(value) for value in values]
    with_times = [
        item
        for item in parsed
        if _clean_text(item["stdResponseTime"]) or _clean_text(item["stdRepairTime"])
    ]
    candidates = with_times or [item for item in parsed if _clean_text(item["normalizedSla"])]
    if not candidates:
        return "", "", ""
    ranked = pd.DataFrame(candidates)
    ranked["Key"] = (
        ranked["stdResponseTime"].astype(str)
        + "|"
        + ranked["stdRepairTime"].astype(str)
        + "|"
        + ranked["normalizedSla"].astype(str)
    )
    selected_key = ranked["Key"].value_counts().idxmax()
    selected = ranked[ranked["Key"].eq(selected_key)].iloc[0]
    return (
        _clean_text(selected["stdResponseTime"]),
        _clean_text(selected["stdRepairTime"]),
        _clean_text(selected["normalizedSla"]),
    )


def _select_combined_sla(group: pd.DataFrame) -> tuple[str, str, str]:
    for _, row in group.iterrows():
        response = _clean_text(row.get("StdResponseTime"))
        repair = _clean_text(row.get("StdRepairTime"))
        evidence = _clean_text(row.get("SlaEvidence"))
        if response or repair:
            return response, repair, evidence
    evidence = _most_common_nonblank(group.get("SlaEvidence", pd.Series(dtype=str)))
    return "", "", evidence


def parse_customer_sla(value: object) -> dict[str, str]:
    """Parse HelpDesk/DN SLA text to numeric-hour customer template fields.

    The Planning data dictionary marks the standard response/repair columns as
    Float. We therefore store hours rather than text labels.
    """

    raw = _clean_text(value)
    if not raw:
        return {"stdResponseTime": "", "stdRepairTime": "", "normalizedSla": ""}
    text = raw.upper().replace("*", "X")
    text = re.sub(r"BASED ON SALES.*$", "", text)
    text = re.sub(r"\bTMS\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    if not text or text in {"N/A", "NA", "LOW"}:
        return {"stdResponseTime": "", "stdRepairTime": "", "normalizedSla": text}

    hours = ""
    if re.search(r"\bNBD\b|\bNEXT BUSINESS DAY\b|\bNBS\b", text):
        hours = "16"
    else:
        match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:HOURS?|HRS?|HR|H)\b", text)
        if match:
            hours = _format_float(match.group(1))

    if not hours:
        return {"stdResponseTime": "", "stdRepairTime": "", "normalizedSla": text}

    is_recovery = bool(re.search(r"\b(RECOVERY|RECOVER|REPAIR|RESOLVE|RESOLUTION|RECOV|REC)\b", text))
    is_response = bool(re.search(r"\b(RESPONSE|RESPOND|RESP|RESPON)\b", text))
    if is_recovery:
        return {"stdResponseTime": hours, "stdRepairTime": hours, "normalizedSla": text}
    if is_response:
        return {"stdResponseTime": hours, "stdRepairTime": "", "normalizedSla": text}
    return {"stdResponseTime": "", "stdRepairTime": "", "normalizedSla": text}


def _format_float(value: object) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number):
        return ""
    as_float = float(number)
    if as_float.is_integer():
        return str(int(as_float))
    return f"{as_float:g}"


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
        elif object_name in TEMPLATE_OBJECT_DECISIONS:
            decision = TEMPLATE_OBJECT_DECISIONS[object_name]
            status = decision["status"]
            notes = decision["notes"]
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
