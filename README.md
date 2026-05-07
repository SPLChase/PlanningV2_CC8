# PlanningV2

PlanningV2 is the working repo for onboarding CoCre8 data into the Planning Tool v2 import templates.

The first implementation reference is `MinStock-3.0`, which connects to SAP Business One Service Layer, extracts item/warehouse stock facts, enriches them with planning dimensions, and writes upload-ready workbook tables.

## Current Understanding

- SAP source access is through SAP Business One Service Layer.
- Authentication posts to `/Login` with `CompanyDB`, `UserName`, and `Password`.
- The stock extract is run through Service Layer `SQLQueries`: create or patch a saved query, then call `SQLQueries('<code>')/List`.
- Service Layer paging is handled with `odata.nextLink` / `@odata.nextLink`.
- Core stock grain is item + warehouse, with fields for item description, item group, warehouse code, in stock, minimum stock, and ordered quantity.
- Planning enrichment adds SPL master mappings, warehouse/customer dimensions, pricing/return data, usage history, row keys, and row signatures.

See [docs/minstock3-notes.md](docs/minstock3-notes.md) for the detailed notes captured from the source repo.

## Repo Layout

```text
planning_v2/
  config.py              Environment-driven pipeline settings
  sap_service_layer.py   SAP Service Layer session and query helpers
  schemas.py             Shared column definitions for onboarding outputs
docs/
  minstock3-notes.md     Findings from MinStock-3.0
  open-questions.md      Questions to resolve before building imports
data/
  raw/                   Local source extracts, ignored by git
  stage/                 Local intermediate files, ignored by git
  output/                Generated onboarding templates, ignored by git
```

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item planning.env.example planning.env
```

Fill `planning.env` with SAP Service Layer credentials and local data paths.

## Next Build Step

Once the Planning Tool v2 template files and required tab/column definitions are confirmed, add extract/transform jobs that produce those exact template tables from CoCre8's SAP and dimension sources.

## CoCre8 Onboarding V1

Build the human-readable field map from the reference workbooks and local `C:\dev\cc8` evidence:

```powershell
py -m planning_v2.build_field_map --reference-dir Reference --cc8-root C:\dev\cc8
```

Output:

- `docs/field-map/CoCre8_PlanningV2_Field_Map.xlsx`

Generate usable onboarding CSVs from confirmed local sources, plus header-only templates for unresolved objects:

```powershell
py -m planning_v2.generate_onboarding_csvs --out data/output/template_onboarding_csvs
```

Generated CSVs under `data/output/` are ignored by git. The workbook has review tabs for both the original reference field list and the Planning V2 sample-template fields:

- `Target Fields`
- `Template Fields`
- `Source Evidence`
- `Unknowns`
- `Output Objects`
- `SAP Investigation Log`

Current template-shaped CSVs with confirmed partial data are `Parts`, `Warehouses`, `WarehouseStockOnHand`, and `Customers`. The remaining template CSVs are header-only until SAP Service Layer sources are proven.

Run tests:

```powershell
py -m unittest discover -s tests -v
```
