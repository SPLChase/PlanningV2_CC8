"""Generate confirmed Planning V2 onboarding CSVs from CoCre8 local sources."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from planning_v2.config import PlanningConfig, get_config
from planning_v2.field_mapping import clean_text
from planning_v2.schemas import CONFIRMED_OUTPUT_OBJECTS, PENDING_OUTPUT_OBJECTS


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def _write_csv(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0.0)


def _col(df: pd.DataFrame, name: str, default: object = "") -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index)


def _first_existing(*frames: pd.DataFrame) -> pd.DataFrame:
    for frame in frames:
        if not frame.empty:
            return frame
    return pd.DataFrame()


def generate_onboarding_csvs(cfg: PlanningConfig, out_dir: Path) -> list[Path]:
    csv_dir = out_dir
    template_dir = out_dir.parent / "investigation_templates"
    csv_dir.mkdir(parents=True, exist_ok=True)
    template_dir.mkdir(parents=True, exist_ok=True)

    exco = cfg.exco_output_dir
    inventory = _read_csv(exco / "InventoryCurrent.csv")
    usage = _read_csv(exco / "Usage.csv")
    stock_flow = _read_csv(exco / "StockFlow.csv")
    warehouses = _read_csv(exco / "Warehouses.csv")
    customers = _read_csv(exco / "Customers.csv")
    parts = _read_csv(exco / "Parts.csv")

    written: list[Path] = []
    written.append(_write_csv(build_spl_masters(parts), csv_dir / "spl_masters.csv"))
    written.append(_write_csv(build_part_alternatives(parts), csv_dir / "part_alternatives.csv"))
    written.append(_write_csv(build_parts(parts, inventory), csv_dir / "parts.csv"))
    written.append(_write_csv(build_warehouses(warehouses), csv_dir / "warehouses.csv"))
    written.append(_write_csv(build_customers(customers), csv_dir / "customers.csv"))
    written.append(_write_csv(build_stock_detail(inventory), csv_dir / "stock_detail.csv"))
    written.append(_write_csv(build_usage(usage), csv_dir / "usage.csv"))
    written.append(_write_csv(build_stock_flow(stock_flow), csv_dir / "stock_flow.csv"))

    for object_name, columns in PENDING_OUTPUT_OBJECTS.items():
        written.append(_write_csv(pd.DataFrame(columns=columns), template_dir / f"{object_name}.csv"))

    validation = validate_outputs(csv_dir, template_dir)
    written.append(_write_csv(validation, out_dir.parent / "validation_summary.csv"))
    return written


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
    parser.add_argument("--exco-output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = get_config()
    if args.exco_output_dir:
        cfg = PlanningConfig(**{**cfg.__dict__, "exco_output_dir": args.exco_output_dir})
    written = generate_onboarding_csvs(cfg, args.out)
    print(f"Wrote {len(written)} onboarding files under {args.out.parent}")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
