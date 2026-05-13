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


def build_template_item_master_sql() -> str:
    return """
SELECT
    T0."ItemCode" AS "ItemNo",
    T0."ItemName" AS "ItemDescription",
    T0."ItmsGrpCod" AS "ItemsGroupCode"
FROM OITM T0
WHERE T0."InvntItem" = 'Y'
ORDER BY T0."ItemCode"
""".strip()


def build_template_stock_on_hand_sql() -> str:
    return """
SELECT
    T0."ItemCode" AS "ItemNo",
    T1."ItemName" AS "ItemDescription",
    T1."ItmsGrpCod" AS "ItemsGroupCode",
    T0."WhsCode" AS "WarehouseCode",
    T0."OnHand" AS "OnHand",
    T0."IsCommited" AS "IsCommited",
    T0."OnOrder" AS "OnOrder",
    T0."MinStock" AS "MinStock",
    T0."MaxStock" AS "MaxStock",
    T0."AvgPrice" AS "AvgPrice"
FROM OITW T0
INNER JOIN OITM T1 ON T1."ItemCode" = T0."ItemCode"
WHERE
    T1."InvntItem" = 'Y'
    AND (
        T0."OnHand" <> 0
        OR T0."IsCommited" <> 0
        OR T0."OnOrder" <> 0
    )
ORDER BY
    T0."WhsCode", T0."ItemCode"
""".strip()


def build_recent_warehouse_movements_sql(cutoff_date: str) -> str:
    return f"""
SELECT
    M."WarehouseCode" AS "WarehouseCode",
    SUM(M."MovementCount") AS "MovementCount",
    MAX(M."LastMovementDate") AS "LastMovementDate"
FROM (
    SELECT
        D1."WhsCode" AS "WarehouseCode",
        COUNT(*) AS "MovementCount",
        MAX(D0."DocDate") AS "LastMovementDate"
    FROM DLN1 D1
    INNER JOIN ODLN D0 ON D0."DocEntry" = D1."DocEntry"
    WHERE D0."DocDate" >= '{cutoff_date}'
    GROUP BY D1."WhsCode"
    UNION ALL
    SELECT
        G1."WhsCode" AS "WarehouseCode",
        COUNT(*) AS "MovementCount",
        MAX(G0."DocDate") AS "LastMovementDate"
    FROM PDN1 G1
    INNER JOIN OPDN G0 ON G0."DocEntry" = G1."DocEntry"
    WHERE G0."DocDate" >= '{cutoff_date}'
    GROUP BY G1."WhsCode"
) M
GROUP BY M."WarehouseCode"
ORDER BY M."WarehouseCode"
""".strip()


def build_purchase_order_lines_sql() -> str:
    return """
SELECT
    P0."DocEntry" AS "DocEntry",
    P0."DocNum" AS "PurchaseOrderNumber",
    P1."LineNum" AS "LineNum",
    P0."DocStatus" AS "DocStatus",
    P0."CANCELED" AS "Canceled",
    P0."CreateDate" AS "CreationDateTime",
    P0."DocDate" AS "ApprovalDateTime",
    P1."WhsCode" AS "ToWarehouseId",
    P0."CardCode" AS "VendorId",
    P1."ItemCode" AS "PartNumber",
    P1."Quantity" AS "Quantity",
    P1."LineTotal" AS "LineCost"
FROM OPOR P0
INNER JOIN POR1 P1 ON P1."DocEntry" = P0."DocEntry"
WHERE P1."ItemCode" IS NOT NULL
ORDER BY
    P0."DocEntry" DESC, P1."LineNum"
""".strip()


def build_purchase_order_receipts_sql() -> str:
    return """
SELECT
    G1."BaseEntry" AS "DocEntry",
    G1."BaseLine" AS "LineNum",
    SUM(G1."Quantity") AS "QuantityReceived",
    MAX(G0."DocDate") AS "ReceivedDateTime"
FROM PDN1 G1
INNER JOIN OPDN G0 ON G0."DocEntry" = G1."DocEntry"
WHERE
    G1."BaseType" = 22
    AND G0."CANCELED" = 'N'
GROUP BY
    G1."BaseEntry", G1."BaseLine"
ORDER BY
    G1."BaseEntry" DESC, G1."BaseLine"
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
