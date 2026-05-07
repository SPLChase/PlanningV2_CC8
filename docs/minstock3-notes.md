# MinStock-3.0 Notes

## SAP Service Layer Connection

`MinStock-3.0` uses `requests.Session` against SAP Business One Service Layer.

The connection flow is:

1. Load settings from `minstock.env`, `pipeline.env`, or process environment.
2. Require `SAP_URL`, `SAP_COMPANY`, `SAP_USER`, and `SAP_PASSWORD`.
3. `POST {SAP_URL}/Login` with `CompanyDB`, `UserName`, and `Password`.
4. Use the same session for all Service Layer calls.
5. `POST {SAP_URL}/Logout` in a `finally` block.

SSL verification is disabled in the reference pipeline with `verify=False`, and `urllib3` warnings are suppressed.

## Stock Extract

The raw stock extract is not pulled from a standard entity endpoint directly. It uses SAP B1 Service Layer `SQLQueries`:

1. Build SQL over `OITW` and `OITM`.
2. Check `GET /SQLQueries('<SQL_QUERY_CODE>')`.
3. If it exists, patch `SqlText`.
4. If it returns 404, create it with `POST /SQLQueries`.
5. Execute `GET /SQLQueries('<SQL_QUERY_CODE>')/List`.
6. Page through `odata.nextLink` or `@odata.nextLink`.

The query returns item/warehouse facts:

- `ItemCode`
- `ItemName`
- `ItemsGroupCode`
- `WarehouseCode`
- `InStock`
- `OnOrder`
- `MinStock`

`ItemGroups?$select=Number,GroupName` is fetched separately to map item group codes to names.

## Data Grain

The base grain is one row per `ItemCode + WarehouseCode`.

The planning grain can then be widened with:

- item master grouping through `SPLMaster`, `ItemKey`, and `MasterKey`
- warehouse attributes: name and location
- customer mapping for dedicated warehouses
- SPI return/repair/pricing fields
- usage history from stock audit delivery-note rows
- row identity fields: `RowKey` and `RowSig`

## Transform Pipeline

`MinStock-3.0` runs five jobs:

1. `extract_raw_stock.py`: Service Layer extract to `RawStock.xlsx`.
2. `map_masters.py`: joins item codes to SPL masters.
3. `compute_globals.py`: groups by `MasterKey` to calculate global stock/min/ordered and shortage.
4. `enrich_dimensions.py`: adds warehouse, customer, SPI pricing/return fields, and usage history.
5. `build_upload_workbook.py`: writes workbook tables for `Main`, `Dedicated`, `Others`, and `Customers`.

## Output Tables

The final stock table columns are normalized to no-space names:

- part/item identity: `PartNo`, `SPLMaster`, `ItemKey`, `MasterKey`
- dimensions: `ItemDescription`, `GroupName`, `WarehouseCode`, `WarehouseName`, `Location`, `CustomerCode`
- stock facts: `InStock`, `MinimumStock`, `Ordered`
- aggregate facts: `GlobalInStock`, `GlobalMin`, `GlobalOrdered`, `Shortage`
- commercial/service fields: `ReturnCode`, `RepairCode`, `ListPrice`, `CreditPrice`, `CoCre8Cost`
- usage fields: `Used24`, `Used25`, `Used26`
- sync fields: `RowSig`, `RowKey`

Dedicated warehouse global values are recalculated by `CustomerCode + MasterKey`; blank customer codes fall back to warehouse-level grouping.
