# Agent Notes

This repo is for onboarding CoCre8 data into the external Planning V2 Excel/CSV import templates.

## Project Context

- Planning V2 is an AI-powered spare-parts planning tool built by an external agency.
- SPL provides logistics and warehousing fulfilment for CoCre8/Fujitsu service parts.
- The planning tool operates mainly at **SPL Master** level. Actual SAP/email part numbers must still be retained for traceability because requested, dispatched, returned, and replenished parts may be alternatives.
- `Reference/masters.csv` is the canonical bridge from actual part numbers to `SPL Master`.
- `Reference/SPL Planning Data Fields edited.xlsx` is the current field-priority source. It includes user columns such as `Priority`, `CC8Comment`, and `Codex`.
- The canonical import headers come from the individual workbooks in `Samples/` such as `Warehouses.xlsx`, `Warehouse Stock on Hand.xlsx`, and `Purchase Orders.xlsx`. `Templates raw.xlsx` is only a fallback/metadata source when an individual workbook is absent.
- Priority meaning:
  - `1`: MVP.
  - `2`: immediately after MVP / some work needed.
  - `3`: later, harder, or unavailable.
  - `0`: not applicable.
- Do not count `Priority = 0` or `CC8 relevant? = No` fields as blockers.

## Data Source Rules

- Use live SAP Business One Service Layer as the source of truth for SAP data. Do not use saved Exco output CSVs as a shortcut for stock/PO data.
- Allowed file-based exceptions:
  - `SPI_DATA.csv` for main alternative / pricing-style SPI enrichment.
  - Manually generated SAP Stock Audit Report for usage.
  - Manually generated stock ageing report if it becomes relevant.
  - `Reference/masters.csv` for SPL Master mapping.
- MinStock3 is a reference implementation for SAP Service Layer access patterns, especially SQLQueries against SAP B1 tables.
- Current live SAP-generated template outputs include:
  - `Parts.csv`
  - `Warehouses.csv`
  - `WarehouseStockOnHand.csv`
  - `Vendors.csv`
  - `PartsUsage.csv`
  - `PurchaseOrders.csv`
- Generated customer/upload data belongs under `data/output/` and should remain ignored by git.

## Purchase Order Rules

- `PurchaseOrders.csv` should remain SAP-sourced.
- SAP PO number comes from SAP `OPOR.DocNum`.
- HelpDesk ticket `PurchaseOrder` values may be stored as PDF filenames such as `26PO000031.pdf`; the comparable PO value is the filename stem, e.g. `26PO000031`.
- Use HelpDesk PO filename stems for reconciliation evidence only. If the stem does not match SAP `OPOR.DocNum`, flag this as a data-quality issue instead of replacing SAP PO numbers.
- `demandStatus` is not currently proven. Only populate it from HelpDesk `ReplenishStatus` if the HelpDesk PO stem matches SAP PO number and the part or SPL Master also matches.

## HelpDesk / Email Rules

- `Reference/HelpdeskFlow.txt` and `Reference/helpdesk_emails/` are valid local reference inputs and should be inspected when needed.
- Do not commit raw HelpDesk flow exports or email files to GitHub. The flow export has contained a hardcoded OpenAI API key, and the emails contain customer/order data.
- Recommendations based on those files should be committed as sanitized docs only.
- Important lifecycle identifiers to preserve when possible:
  - `MSConvoID`
  - call/ticket number
  - requested part number
  - dispatched part number
  - returned faulty part number
  - replenishment part number
  - CoCre8 PO filename stem
  - SAP PO number
  - Fujitsu order confirmation number
  - delivery note number
- Use strict evidence matching only. No fuzzy matching by customer/subject/date unless explicitly approved by the user.

## SPL Master / Alternatives

- Always think in two grains:
  - actual part number from SAP, email, ticket, or report;
  - mapped `SPL Master` for planning.
- Do not treat an alternative replenishment part as a mismatch if it maps to the same `SPL Master`.
- Do not silently collapse alternatives. Evidence outputs should make requested, dispatched, and replenished part numbers visible.
- Rows with no `SPL Master` mapping should be flagged for investigation.

## Template-Specific Notes

- `Parts.csv`:
  - `isPrimary` comes from `SPI_DATA.csv`: if `Main alternative par` equals the part number, `isPrimary = True`; otherwise false when known.
  - `SPLMaster` comes from `masters.csv`.
  - Product class/type/part type may come from SAP item group or related item master fields, but exact Planning V2 semantics are not confirmed.
- `WarehouseStockOnHand.csv`:
  - `partCode` maps to SAP `OITW.ItemCode`.
  - `quantityAllocated` maps to SAP `OITW.IsCommited`.
  - `quantityOnHand` maps to SAP `OITW.OnHand`.
  - `quantityInbound` maps to SAP `OITW.OnOrder`.
  - `inventoryType` and `quantityOutbound` are not needed for CoCre8 MVP unless the user reverses this.
  - `uniqueId` may be derived as a stable integer from a MinStock-style row key: `part|SPLMaster|warehouse`. MinStock3 `RowKey` itself is a string (`PartNo|MasterKey|Warehouse Code`), while the Planning V2 template expects an integer.
- `Vendors.csv`:
  - `vendorId` maps to SAP `OPOR.CardCode` for vendors present in PO history.
  - `Description` maps to `OCRD.CardName`.
  - `isActive` maps to `OCRD.validFor`, normalized to `Y`/`N`.
- `Warehouses.csv`:
  - SAP supplies `warehouseId` and `warehouseDescription`.
  - Many remaining fields are manual/not applicable for CoCre8; provide a simple fill-in workbook when needed.
  - Do not remove unused, empty, virtual, faulty-return, or quarantine warehouses from the manual fill-in workbook. The data dictionary indicates unused/empty sites should be marked obsolete instead.
  - If `supplyWarehouseId` is blank in the user's fill-in workbook, treat that as a signal that the warehouse is virtual/quarantine/faulty-return/non-replenishable, not as a reason to delete the row.
  - `warehouseStatusId` is the active flag / newer name for active status. Map from manual `isObsolete`: `isObsolete = Y` -> `warehouseStatusId = N`; `isObsolete = N` -> `warehouseStatusId = Y`.
- `PartsUsage.csv`:
  - Delivery Notes / SAP stock movement are the source of truth for actual parts usage because requested HelpDesk parts often differ from dispatched/used alternatives, and HelpDesk tickets do not always exist.
  - Use Stock Audit Report DN rows for actual `partCode`, `quantityUsed`, `Warehouse`, and `partsUsedDateTime`.
  - Use `masters.csv` as the primary source for `Master` on the actual used part.
  - Do not use Work Order Number for `orderNumber` or `requestId`. If those fields are later enriched, use HelpDesk call/ticket number only when there is strict evidence linking the DN/SAP usage row to the ticket.
  - Leave HelpDesk-only fields such as customer, serial, and order start blank unless a strict DN-to-ticket/SAP-service link is proven.
- `PurchaseOrders.csv`:
  - Use live SAP PO lines and receipts.
  - HelpDesk export is reconciliation/enrichment evidence, not a replacement source.

## Workbook / Spreadsheet Generation

- Do not use openpyxl Excel Tables (`worksheet.add_table(...)`) for generated `.xlsx` review/fill-in workbooks unless the user explicitly asks for table objects.
- Do not set `worksheet.auto_filter.ref` for simple generated workbooks. Excel has repeatedly repaired generated files by removing table/autofilter metadata.
- Prefer robust plain worksheets: styled header row, frozen panes, column widths, wrapped text, and normal cell ranges that the user can filter manually.
- Avoid named ranges for validation lists unless necessary. If validation is useful, prefer simple inline list validations such as `"TRUE,FALSE,N/A"`.
- Before handing over a workbook, reopen it with `openpyxl.load_workbook(...)` as a smoke check.
- If a workbook save fails with `PermissionError`, it is probably open in Excel. Write to an alternate filename only if needed and tell the user clearly.

## Git / Output Policy

- Commit and push after each substantive change set over roughly 10 changed lines.
- Do not commit generated customer data under `data/output/`.
- Do commit code, tests, sanitized docs, and review workbooks that are intended as project artifacts.
- Do not commit secrets, raw HelpDesk exports, raw `.eml` files, or local credentials.
- If GitHub push fails, report the exact error and local commit SHA.

## Useful Commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m planning_v2.build_field_map --reference-dir Reference --cc8-root C:\dev\cc8
.\.venv\Scripts\python.exe -m planning_v2.generate_onboarding_csvs --out data\output\onboarding_csvs
```

If SAP generation fails with `splapp01.spl.local` DNS errors, check VPN/DNS before changing code.
