"""Shared column definitions for CoCre8 onboarding outputs."""

RAW_STOCK_COLUMNS = [
    "ItemCode",
    "ItemName",
    "ItemsGroupCode",
    "GroupName",
    "WarehouseCode",
    "InStock",
    "MinStock",
    "OnOrder",
]

MINSTOCK3_FINAL_COLUMNS = [
    "PartNo",
    "ItemDescription",
    "GroupName",
    "WarehouseCode",
    "WarehouseName",
    "Location",
    "CustomerCode",
    "SPLMaster",
    "InStock",
    "MinimumStock",
    "GlobalInStock",
    "GlobalMin",
    "Shortage",
    "Ordered",
    "GlobalOrdered",
    "ItemKey",
    "MasterKey",
    "ReturnCode",
    "RepairCode",
    "ListPrice",
    "CreditPrice",
    "Used24",
    "Used25",
    "Used26",
    "CoCre8Cost",
    "RowSig",
    "RowKey",
]

CUSTOMER_COLUMNS = [
    "CustomerCode",
    "WarehouseCode",
    "GroupName",
    "WarehouseName",
    "Location",
]
