"""Live SAP extracts used by Planning V2 onboarding CSV generation."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from planning_v2.config import PlanningConfig
from planning_v2.sap_queries import (
    build_delivery_note_usage_sql,
    build_purchase_order_lines_sql,
    build_purchase_order_receipts_sql,
    build_recent_warehouse_movements_sql,
    build_template_item_master_sql,
    build_template_stock_on_hand_sql,
)
from planning_v2.sap_service_layer import SapServiceLayer


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "<na>"}:
        return ""
    return text


def _to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0.0)


def _lookup_frame(rows: list[dict], code_column: str, name_column: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=[code_column, name_column])
    df = pd.DataFrame(rows)
    for column in [code_column, name_column]:
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].map(_clean_text)
    return df[[code_column, name_column]].drop_duplicates(subset=[code_column], keep="first")


def fetch_live_template_sources(cfg: PlanningConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch item, warehouse, and stock facts directly from SAP Service Layer.

    This intentionally avoids the saved Exco output CSVs. SPI and manual SAP
    report files are still handled by their specific parsers elsewhere.
    """

    with SapServiceLayer(cfg) as sap:
        sap.ensure_sql_query(f"{cfg.sql_query_code}_ITEMS", build_template_item_master_sql())
        sap.ensure_sql_query(f"{cfg.sql_query_code}_STOCK_ON_HAND", build_template_stock_on_hand_sql())

        item_rows = sap.run_sql_query(f"{cfg.sql_query_code}_ITEMS")
        stock_rows = sap.run_sql_query(f"{cfg.sql_query_code}_STOCK_ON_HAND")
        warehouse_rows = sap.fetch_lookup("Warehouses", ["WarehouseCode", "WarehouseName"])
        group_rows = sap.fetch_lookup("ItemGroups", ["Number", "GroupName"])

    item_groups = {
        int(row["Number"]): _clean_text(row.get("GroupName"))
        for row in group_rows
        if row.get("Number") is not None
    }
    warehouse_lookup = _lookup_frame(warehouse_rows, "WarehouseCode", "WarehouseName")

    items = pd.DataFrame(item_rows)
    if items.empty:
        items = pd.DataFrame(columns=["ItemNo", "ItemDescription", "ItemsGroupCode", "GroupName"])
    for column in ["ItemNo", "ItemDescription"]:
        if column not in items.columns:
            items[column] = ""
        items[column] = items[column].map(_clean_text)
    if "ItemsGroupCode" not in items.columns:
        items["ItemsGroupCode"] = ""
    items["GroupName"] = pd.to_numeric(items["ItemsGroupCode"], errors="coerce").map(item_groups).fillna("")

    stock = pd.DataFrame(stock_rows)
    if stock.empty:
        stock = pd.DataFrame(
            columns=[
                "ItemNo",
                "ItemDescription",
                "ItemsGroupCode",
                "GroupName",
                "WarehouseCode",
                "WarehouseName",
                "OnHand",
                "IsCommited",
                "OnOrder",
                "MinStock",
                "MaxStock",
                "AvgPrice",
            ]
        )
    for column in ["ItemNo", "ItemDescription", "WarehouseCode"]:
        if column not in stock.columns:
            stock[column] = ""
        stock[column] = stock[column].map(_clean_text)
    if "ItemsGroupCode" not in stock.columns:
        stock["ItemsGroupCode"] = ""
    stock["GroupName"] = pd.to_numeric(stock["ItemsGroupCode"], errors="coerce").map(item_groups).fillna("")
    for column in ["OnHand", "IsCommited", "OnOrder", "MinStock", "MaxStock", "AvgPrice"]:
        if column not in stock.columns:
            stock[column] = 0
        stock[column] = _to_number(stock[column])
    stock = stock.merge(warehouse_lookup, on="WarehouseCode", how="left")
    stock["WarehouseName"] = stock["WarehouseName"].fillna("")

    stock_warehouses = (
        stock[["WarehouseCode", "WarehouseName"]]
        .drop_duplicates(subset=["WarehouseCode"], keep="first")
        .sort_values("WarehouseCode")
        .reset_index(drop=True)
    )

    return items.reset_index(drop=True), stock_warehouses, stock.reset_index(drop=True)


def fetch_recent_warehouse_movements(cfg: PlanningConfig, today: date | None = None) -> pd.DataFrame:
    cutoff = ((today or date.today()) - timedelta(days=365)).isoformat()
    with SapServiceLayer(cfg) as sap:
        sap.ensure_sql_query(f"{cfg.sql_query_code}_RECENT_WH_MOVES", build_recent_warehouse_movements_sql(cutoff))
        rows = sap.run_sql_query(f"{cfg.sql_query_code}_RECENT_WH_MOVES")
    moves = pd.DataFrame(rows)
    if moves.empty:
        return pd.DataFrame(columns=["WarehouseCode", "MovementCount", "LastMovementDate"])
    for column in ["WarehouseCode", "LastMovementDate"]:
        if column not in moves.columns:
            moves[column] = ""
        moves[column] = moves[column].map(_clean_text)
    if "MovementCount" not in moves.columns:
        moves["MovementCount"] = 0
    moves["MovementCount"] = _to_number(moves["MovementCount"])
    return moves.drop_duplicates(subset=["WarehouseCode"], keep="first").reset_index(drop=True)


def fetch_live_delivery_note_usage(cfg: PlanningConfig, today: date | None = None) -> pd.DataFrame:
    cutoff = ((today or date.today()) - timedelta(days=365 * 3)).isoformat()
    with SapServiceLayer(cfg) as sap:
        sap.ensure_sql_query(f"{cfg.sql_query_code}_DN_USAGE", build_delivery_note_usage_sql(cutoff))
        rows = sap.run_sql_query(f"{cfg.sql_query_code}_DN_USAGE")

    usage = pd.DataFrame(rows)
    if usage.empty:
        return pd.DataFrame(
            columns=[
                "DocEntry",
                "DeliveryNoteNumber",
                "DocDate",
                "DocStatus",
                "CardCode",
                "CardName",
                "CustomerRefNumber",
                "Comments",
                "BillToAddress",
                "ShipToAddress",
                "ShipToCode",
                "PayToCode",
                "LineNum",
                "ItemNo",
                "ItemDescription",
                "Quantity",
                "WarehouseCode",
            ]
        )

    for column in [
        "DeliveryNoteNumber",
        "DocDate",
        "DocStatus",
        "CardCode",
        "CardName",
        "CustomerRefNumber",
        "Comments",
        "BillToAddress",
        "ShipToAddress",
        "ShipToCode",
        "PayToCode",
        "ItemNo",
        "ItemDescription",
        "WarehouseCode",
    ]:
        if column not in usage.columns:
            usage[column] = ""
        usage[column] = usage[column].map(_clean_text)
    for column in ["DocEntry", "LineNum"]:
        if column in usage.columns:
            usage[column] = pd.to_numeric(usage[column], errors="coerce").fillna(-1).astype(int)
    if "Quantity" not in usage.columns:
        usage["Quantity"] = 0
    usage["Quantity"] = _to_number(usage["Quantity"])
    return usage.reset_index(drop=True)


def fetch_live_purchase_orders(cfg: PlanningConfig) -> pd.DataFrame:
    with SapServiceLayer(cfg) as sap:
        sap.ensure_sql_query(f"{cfg.sql_query_code}_PO_LINES", build_purchase_order_lines_sql())
        sap.ensure_sql_query(f"{cfg.sql_query_code}_PO_RECEIPTS", build_purchase_order_receipts_sql())
        line_rows = sap.run_sql_query(f"{cfg.sql_query_code}_PO_LINES")
        receipt_rows = sap.run_sql_query(f"{cfg.sql_query_code}_PO_RECEIPTS")

    lines = pd.DataFrame(line_rows)
    if lines.empty:
        return pd.DataFrame(
            columns=[
                "DocEntry",
                "SapInternalPurchaseOrderNumber",
                "PurchaseOrderNumber",
                "PurchaseOrderComments",
                "LineNum",
                "DocStatus",
                "Canceled",
                "CreationDateTime",
                "ApprovalDateTime",
                "ToWarehouseId",
                "VendorId",
                "PartNumber",
                "Quantity",
                "LineCost",
                "QuantityReceived",
                "ReceivedDateTime",
            ]
        )

    receipts = pd.DataFrame(receipt_rows)
    if receipts.empty:
        receipts = pd.DataFrame(columns=["DocEntry", "LineNum", "QuantityReceived", "ReceivedDateTime"])

    for frame in [lines, receipts]:
        for column in ["DocEntry", "LineNum"]:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(-1).astype(int)

    merged = lines.merge(receipts, on=["DocEntry", "LineNum"], how="left")
    for column in [
        "PurchaseOrderNumber",
        "SapInternalPurchaseOrderNumber",
        "PurchaseOrderComments",
        "DocStatus",
        "Canceled",
        "CreationDateTime",
        "ApprovalDateTime",
        "ToWarehouseId",
        "VendorId",
        "PartNumber",
        "ReceivedDateTime",
    ]:
        if column not in merged.columns:
            merged[column] = ""
        merged[column] = merged[column].map(_clean_text)
    merged["PurchaseOrderNumber"] = merged["PurchaseOrderNumber"].where(
        merged["PurchaseOrderNumber"].astype(str).str.strip().ne(""),
        merged["SapInternalPurchaseOrderNumber"],
    )
    for column in ["Quantity", "LineCost", "QuantityReceived"]:
        if column not in merged.columns:
            merged[column] = 0
        merged[column] = _to_number(merged[column])
    return merged.reset_index(drop=True)
