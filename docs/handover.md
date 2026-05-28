# CoCre8 Planning V2 Onboarding Handover

Last updated: 2026-05-18  
Branch: `onboarding-field-map`  
Last known commit before this handover: `7fe4175`

## Project Aim

Build CoCre8 onboarding CSVs for the external Planning V2 tool using confirmed data only. The planning tool mainly operates at `SPL Master` level, but upload templates often require actual part ids, so keep actual SAP/Fujitsu part numbers in the upload and keep `SPL Master` linkage in review evidence where the template has no master field.

## Repo Setup

Clone and switch to the working branch:

```powershell
cd C:\dev
git clone https://github.com/SPLChase/PlanningV2_CC8.git
cd PlanningV2_CC8
git checkout onboarding-field-map
```

Install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use the repo venv for tests. Global `pytest` may not be installed:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Generate outputs:

```powershell
.\.venv\Scripts\python.exe -m planning_v2.generate_onboarding_csvs --out data/output/onboarding_csvs
.\.venv\Scripts\python.exe -m planning_v2.build_field_map --reference-dir Reference --cc8-root C:\dev\cc8
```

SAP Service Layer calls require VPN. `planning.env` contains placeholders; config falls back to MinStock3 env files at `C:\dev\cc8\MinStock3\minstock.env` or `pipeline.env` if placeholders are present.

## Important Git/Data Rules

- Generated customer/upload data under `data/output` is ignored and not committed.
- The generated review evidence under `data/output/review_evidence` is also ignored; regenerate it locally.
- Code, docs, tests, reference workbooks, sample templates, and tracked historical SPI files are committed.
- Commit and push substantive changes to `onboarding-field-map`.
- Avoid using Excel tables/autofilters in generated workbooks; see `agents.md` for the Excel repair warning.

## Canonical Inputs

- `Samples/`: canonical Planning V2 upload templates and headers.
- `Reference/SPL Planning Data Fields edited.xlsx`: user-edited field priority, relevance, comments, and Codex notes.
- `Reference/masters.csv`: canonical bridge from actual part number to `SPL Master`.
- Current SPI file: `C:\dev\cc8\Exco\source_data\SPI_DATA.csv`.
- Historical SPI price fallback: `Reference/SPI_Historical/`.
- HelpDesk ticket export default path: `C:\Users\richa\Downloads\CoCre8 Issue Tracker V3.2.csv`.
- HelpDesk flow reference: `Reference/HelpdeskFlow.txt`.
- Typical helpdesk email threads: `Reference/helpdesk_emails/`.
- Live SAP Service Layer via MinStock3 credentials.

## Core Source-Of-Truth Decisions

- Stock and item master data must come from live SAP Service Layer, not stale Exco output CSVs.
- Exception: SPI data and manually generated SAP reports may be file-based.
- `PartsUsage.csv` source of truth is SAP Delivery Notes (`ODLN/DLN1`), not HelpDesk, because the dispatched part may be an alternative.
- HelpDesk is used only for strict enrichment where an unambiguous call number matches.
- `PurchaseOrders.csv.purchaseOrderNumber` must use CoCre8/Fujitsu PO numbers from SAP `OPOR.NumAtCard`, or a PO-like value parsed from `OPOR.Comments`. Do not use SAP `DocNum` in the upload-facing field.
- SAP `DocNum` is retained only in review evidence for audit traceability.
- `PurchaseOrders.csv.lineCost` must not use SAP `POR1.LineTotal`; those values are not reliable.
- CoCre8 cost is `SPI ListPrice * 0.72`.
- `PartCost.csv` uses actual part id as `partCode`; `SPL Master` mapping is evidence only.
- `PartCost.csv.cost` is the SPI-derived cost as of the part's last PO month, falling back to latest available SPI cost.
- `PartCost.csv.averageCost` is PO-quantity-weighted.
- `PartCost.csv.currencyCode` is `EUR`.

## Current Template Status

Latest generated status from `data/output/validation_summary.csv`:

| Template | Rows | Status | Notes |
|---|---:|---|---|
| `Warehouses.csv` | 43 | PASS | Manual fill applied; `warehouseStatusId` is active flag. |
| `WarehouseStockOnHand.csv` | 3454 | PASS | Live SAP stock metrics populated. |
| `PurchaseOrders.csv` | 6006 | PASS | Complete with available data; remaining PO/cost gaps accepted by user. |
| `PartCost.csv` | 1864 | PASS | SPI/historical SPI cost rules applied. |
| `Vendors.csv` | generated from PO vendors | MAPPED | SAP PO vendor codes joined to business partner name/active flag. |
| `InventoryTransfers.csv` | generated from stock audit IM rows | PARTIAL | Posted transfer pairs populated; `movementType` and `shipListCode` remain unresolved. |
| `Parts.csv` | 6061 | PARTIAL | Only business-rule fields remain unresolved. |
| `PartsUsage.csv` | 879 | PARTIAL | Only `OrderType` remains in-scope unresolved. |
| `ActionGroups.csv` | 0 | OUT_OF_SCOPE | All fields out of CoCre8 v1 scope. |
| Other templates | 0 | PENDING | Header-only until source/mapping is agreed. |

Overall count: 4 PASS, 2 PARTIAL, 12 PENDING, 1 OUT_OF_SCOPE.

## Implemented Upload Files

### Warehouses.csv

Source:
- Live SAP warehouse lookup for `warehouseId` and description.
- Manual fill workbook under `docs/manual-fill`.

Key decisions:
- `warehouseStatusId` is active flag: `Y` active, inverse of old `isObsolete`.
- Virtual/quarantine/faulty warehouses are not excluded just because empty; mark active/obsolete as supplied by user.

### WarehouseStockOnHand.csv

Source:
- Live SAP `OITW/OITM`.

Mappings:
- `partCode`: SAP item
- `warehouseCode`: SAP warehouse
- `quantityAllocated`: `OITW.IsCommited`
- `quantityOnHand`: `OITW.OnHand`
- `quantityInbound`: `OITW.OnOrder`
- `uniqueId`: stable derived RowKey-like integer from part/master/warehouse

MVP exclusions:
- `inventoryType`
- `quantityOutbound`

### PurchaseOrders.csv

Source:
- Live SAP `OPOR/POR1`.
- GRPO receipt info from `OPDN/PDN1`.
- HelpDesk only for strict `demandStatus` evidence.
- SPI current and historical files for line cost.

Mappings:
- `purchaseOrderNumber`: `OPOR.NumAtCard`, fallback only to `xxPOxxxxxx` parsed from `OPOR.Comments`
- `purchaseOrderStatus`: SAP status/cancelled mapped to accepted/fulfilled/cancelled
- `creationDateTime`: `OPOR.CreateDate`
- `approvalDateTime`: `OPOR.DocDate`
- `toWarehouseId`: `POR1.WhsCode`
- `vendorId`: `OPOR.CardCode`
- `partNumber`: `POR1.ItemCode`
- `quantity`: `POR1.Quantity`
- `lineCost`: SPI `ListPrice * 0.72`, using price as of PO month where possible
- `quantityReceived`: summed GRPO quantity
- `receivedDateTime`: latest GRPO date
- `demandStatus`: HelpDesk `ReplenishStatus` only when exact PO and part/SPL Master evidence matches

Accepted limitations:
- Some rows still lack customer PO reference or SPI cost, but user confirmed we will not get more data than this. Do not block validation on those row-level gaps.

Evidence:
- `data/output/review_evidence/PurchaseOrders_SPLMaster_Evidence.csv`
- `data/output/review_evidence/PurchaseOrders_Ticket_Reconciliation.csv`

### Vendors.csv

Source:
- Live SAP PO vendors from `OPOR.CardCode`.
- Business partner lookup joined from `OCRD`.

Mappings:
- `vendorId`: `OPOR.CardCode`
- `Description`: `OCRD.CardName`
- `isActive`: `OCRD.validFor`, normalized to `Y`/`N`

Notes:
- This maps vendors actually present in the SAP PO extract, not every SAP vendor.
- No local-only reference files are required.

### InventoryTransfers.csv

Source:
- `Reference/Stock Audit Report.txt`.
- Rows where `Document` starts with `IM `.

Pairing logic:
- Forward-fill `Item No.` from stock audit item header rows.
- Group by `Document + Item No. + abs(Quantity)`.
- Negative quantity row is `fromWarehouseId`.
- Positive quantity row is `toWarehouseId`.
- If a group has equal multiple negative and positive rows, pair by row order.
- If a group is single-sided or imbalanced, skip it rather than guessing.

Current parse result from the committed report:
- 1,877 IM rows.
- 856 generated transfer rows.
- 840 clean one-to-one transfer rows.
- 16 duplicate same-document/item/quantity rows paired by row order.
- Single-sided or imbalanced groups are skipped.

Mappings:
- `inventoryTransferImoId`: `Document|Item No.|abs(quantity)`, with a sequence suffix for duplicate same-document/item/quantity pairs.
- `createdDateTime`: `Posting Date`.
- `completedDateTime`: `Posting Date`.
- `fromWarehouseId`: negative IM row `Whse`.
- `toWarehouseId`: positive IM row `Whse`.
- `demandStatus`: `Fulfilled` for posted paired IM rows.
- `orderStatusIsClosed`: `Y` for posted paired IM rows.
- `isResolved`: `Y` for posted paired IM rows.
- `partNumber`: stock audit `Item No.`.
- `quantity`: absolute IM quantity.

Known CoCre8 transfer routing:
- JHB main warehouse is `FUJITSU`.
- Cape Town warehouse is `FUJ CT`.
- JHB to CPT is posted as two legs: `FUJITSU -> FRANCOIS`, then `FRANCOIS -> FUJ CT`.
- CPT to JHB is posted as two legs: `FUJ CT -> MATTHEW`, then `MATTHEW -> FUJITSU`.
- Do not collapse those legs unless the Planning V2 importer explicitly wants logical end-to-end transfers instead of posted movements.

Remaining unresolved:
- `movementType`: no definition found in the reference files; mark for investigation.
- `shipListCode`: not present in the stock audit report.

MVP exclusion:
- `addressId`: likely an internal/stored address id; exclude until a Service Layer source is obvious.

### PartCost.csv

Source:
- Current SPI file and `Reference/SPI_Historical`.
- Live SAP PO history for last PO month and quantity weighting.

Mappings:
- `partCode`: actual part id
- `cost`: last SPI CoCre8 cost as of last PO month, fallback latest available cost
- `currencyCode`: `EUR`
- `averageCost`: PO quantity-weighted average
- `averageRepairCost`: blank and out of scope

Evidence:
- `data/output/review_evidence/PartCost_Evidence.csv`
- Includes `SPLMaster`, selected source file/date, weighted quantity, weighted rows, and evidence status.

### Parts.csv

Source:
- Live SAP item master for part number and description.
- `Reference/masters.csv` for `SPLMaster` when available.
- SPI for main alternative part where present.

Implemented decisions:
- If no `SPLMaster` in `masters.csv`, leave blank; this means no master exists and is considered complete.
- If SPI has no main alternative part listed, that part is the main alternative.
- `isPrimary`: fully populated.
- `primaryPartNumber`: fully populated; defaults to own part number.
- Blank descriptions become literal `null`.
- `isBootStockable`: `N` for all CoCre8 stock.
- `isBranchStockable`: `Y` for all CoCre8 stock.
- `isObsolete`: `N` for all rows for now.
- `isExcludeFromReplenishment`: `N` for all rows for now.
- `purchaseLeadTimeDays`: `3` by default; `180` if description contains `BBU`.

Remaining unresolved fields:
- `productClass`
- `productType`
- `costCategory`
- `partType`
- `isKit`
- `isCritical`

Notes:
- `isKit` likely depends on `partType` or a separate business rule.
- `productClass`, `productType`, and `costCategory` need business clarification.
- `isCritical` needs a business rule.

### PartsUsage.csv

Source:
- SAP Delivery Notes `ODLN/DLN1` are the source of truth.
- DN remarks are parsed for customer and serial where available.
- HelpDesk enriches only by unambiguous call-number match.

Mappings:
- `orderNumber`: call/customer ref from SAP DN (`ODLN.NumAtCard` or parsed Call Nr), fallback DN number
- `requestId`: call/customer ref where available
- `customerCompanyCode`: parsed `Customer:` from DN remarks
- `orderStartDatetime`: HelpDesk `Created` where call number matches
- `orderStatus`: HelpDesk `Status` where call number matches
- `partCode`: actual delivered SAP part `DLN1.ItemCode`
- `serialNumber` and `deviceSerialNumber`: parsed serial from DN remarks
- `quantityUsed`: `DLN1.Quantity`
- `partsUsedDateTime`: DN date
- `Warehouse`: DN line warehouse
- `Master`: mapped from actual delivered part via `masters.csv`

MVP exclusions:
- `resolvedDateTime`
- `relCompanyId`
- `assignedPersonCode`

Remaining unresolved field:
- `OrderType`: user expects this can probably default to one value, but value is not confirmed yet.

Evidence:
- `data/output/review_evidence/PartsUsage_SPLMaster_Evidence.csv`

## Pending Templates

These are still header-only:

- `Nodes.csv`: planning hierarchy not defined.
- `Customers.csv`: customer/site master semantics unresolved.
- `Addresses.csv`: address object/source not agreed.
- `Employees.csv`: assigned person/action group not available for MVP.
- `ServiceOrder.csv`: populated from `spares_issued2.xlsx` service-call rows plus HelpDesk SLA/location evidence. True resolve/recall fields are still unavailable and remain blank/out of scope.
- `RepairOrder.csv`: returns/faulty/repair lifecycle not reliably structured.
- `Models.csv`: installed base/model source not confirmed.
- `PartTypes.csv`: taxonomy not confirmed.
- `Yields.csv`: no confirmed source.
- `WarehouseExclusions.csv`: generate stock-pooling exclusions from the confirmed CoCre8 interchangeability pools. FUJITSU/FUJ CT/FSCGREEN/FSCGRNCT share stock, Massmart warehouses share stock, Royal Swazi warehouses share stock, and all other cross-pool warehouse pairs are excluded.

## HelpDesk Flow Notes

Existing recommendation doc:

```text
docs/helpdesk-flow-recommendations.md
```

Important future improvements:

- Persist line-level `SPL Master`.
- Persist requested part, dispatched part, returned/faulty part, and replenishment part separately.
- Store stable `MSConvoID`, call number, customer PO, Fujitsu confirmation/order number, and delivery note number.
- Separate statuses for order, dispatch, return, replenishment request, PO approval, order confirmation, receipt, and transfer.
- Rotate/remove hardcoded OpenAI API key from the flow.

## Common Pitfalls

- Do not use HelpDesk tickets as the source of truth for usage. Tickets often contain requested part, not actual dispatched alternative.
- Do not use Work Order Number for `PartsUsage.orderNumber` or `requestId`.
- Do not use SAP `OPOR.DocNum` as upload `purchaseOrderNumber`.
- Do not use SAP PO `LineTotal` for CoCre8 cost.
- Do not treat blank `SPLMaster` in `Parts.csv` as a blocker; blank means no master exists.
- Do not exclude empty/unused warehouses from the fill sheet; mark status instead.
- Do not commit generated `data/output` files unless explicitly requested.

## Suggested Next Steps

1. Confirm `PartsUsage.OrderType` default value, then populate it and move `PartsUsage.csv` to PASS.
2. Get business rules for the remaining `Parts.csv` fields:
   - `productClass`
   - `productType`
   - `costCategory`
   - `partType`
   - `isKit`
   - `isCritical`
3. Decide whether any pending templates are needed for MVP upload or can remain deferred.
4. If further `ServiceOrder` lifecycle fields become MVP, use HelpDesk and SAP only with strict identifiers; avoid fuzzy matching.

## Current Test Status

Latest successful run:

```text
35 passed
```

