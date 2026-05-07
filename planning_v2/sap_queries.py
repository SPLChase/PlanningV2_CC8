"""SAP SQL query builders and investigation candidates for Planning V2."""

from __future__ import annotations

from datetime import date, timedelta

from planning_v2.config import PlanningConfig


def build_stock_sql(cfg: PlanningConfig) -> str:
    return f"""
SELECT
    T0."ItemCode" AS "ItemCode",
    T2."ItemName" AS "ItemName",
    T2."ItmsGrpCod" AS "ItemsGroupCode",
    T0."WhsCode" AS "WarehouseCode",
    T0."OnHand" AS "InStock",
    T0."OnOrder" AS "OnOrder",
    T0."MinStock" AS "MinStock"
FROM OITW T0
INNER JOIN OITM T2 ON T2."ItemCode" = T0."ItemCode"
WHERE
    T2."InvntItem" = 'Y'
    AND T2."ItmsGrpCod" BETWEEN {cfg.group_min} AND {cfg.group_max}
ORDER BY
    T0."ItemCode", T0."WhsCode"
""".strip()


def build_open_po_sql(cfg: PlanningConfig, today: date | None = None) -> str:
    cutoff_date = ((today or date.today()) - timedelta(days=90)).isoformat()
    return f"""
SELECT
    P1."ItemCode" AS "ItemCode",
    P1."WhsCode" AS "WarehouseCode",
    SUM(P1."OpenQty") AS "OpenPOQty"
FROM POR1 P1
INNER JOIN OPOR P0 ON P0."DocEntry" = P1."DocEntry"
INNER JOIN OITM T2 ON T2."ItemCode" = P1."ItemCode"
WHERE
    P0."CANCELED" = 'N'
    AND P0."DocStatus" = 'O'
    AND P0."DocDate" >= '{cutoff_date}'
    AND P1."LineStatus" = 'O'
    AND P1."OpenQty" > 0
    AND T2."InvntItem" = 'Y'
    AND T2."ItmsGrpCod" BETWEEN {cfg.group_min} AND {cfg.group_max}
GROUP BY
    P1."ItemCode",
    P1."WhsCode"
ORDER BY
    P1."ItemCode",
    P1."WhsCode"
""".strip()


SAP_INVESTIGATION_CANDIDATES = [
    {
        "object": "work_order_information",
        "candidate": "ServiceCalls / Orders / SQLQueries",
        "fields": "work order, customer/site, serial, open/resolve timestamps",
    },
    {
        "object": "service_call_sla",
        "candidate": "ServiceCalls / Activities / SQLQueries",
        "fields": "ETA, recall, resolve, SLA clocks, SLA miss reason",
    },
    {
        "object": "internal_inventory_movements",
        "candidate": "InventoryTransferRequests / StockTransfers / SQLQueries",
        "fields": "transfer id, status, from/to warehouse, part, quantity, linked work order",
    },
    {
        "object": "purchase_orders",
        "candidate": "PurchaseOrders / OPOR / POR1 / GoodsReceiptPO",
        "fields": "PO number, vendor, status, warehouse, part, qty, cost, receipt date",
    },
    {
        "object": "repair_orders",
        "candidate": "ServiceCalls / Inventory transfers / user tables",
        "fields": "repair type, item received/resolved date, repair code/status, related work order",
    },
    {
        "object": "installed_base",
        "candidate": "EquipmentCards / SerialNumbers / ProductTrees",
        "fields": "model, serial, customer/site, BOM component, quantity",
    },
    {
        "object": "warehouse_hierarchy",
        "candidate": "Warehouses / Branches / user tables",
        "fields": "warehouse type, feeder hierarchy, technician/action group, lead time",
    },
]
