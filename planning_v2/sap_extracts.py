"""Live SAP extracts used by Planning V2 onboarding CSV generation."""

from __future__ import annotations

import pandas as pd

from planning_v2.config import PlanningConfig
from planning_v2.sap_queries import build_template_item_master_sql, build_template_stock_on_hand_sql
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
