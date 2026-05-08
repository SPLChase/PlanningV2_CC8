from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planning_v2.onboarding_csvs import build_template_parts, build_template_parts_usage, build_template_stock_on_hand, build_stock_detail, validate_outputs
from planning_v2.schemas import CONFIRMED_OUTPUT_OBJECTS, PENDING_OUTPUT_OBJECTS


class OnboardingCsvTests(unittest.TestCase):
    def test_stock_detail_numeric_fields_are_coerced(self) -> None:
        inventory = pd.DataFrame(
            {
                "ItemNo": ["A1"],
                "WarehouseCode": ["WH1"],
                "Quantity": ["2"],
                "Commited": ["1"],
                "Ordered": ["3"],
                "Minimum Stock": ["4"],
                "Maximum Stock": ["5"],
            }
        )
        stock = build_stock_detail(inventory)
        self.assertEqual(stock.loc[0, "qty"], 2)
        self.assertEqual(stock.loc[0, "in_bound_qty"], 3)

    def test_template_stock_on_hand_uses_template_column_names(self) -> None:
        inventory = pd.DataFrame(
            {
                "ItemNo": ["A1"],
                "WarehouseCode": ["WH1"],
                "Quantity": ["2"],
                "Commited": ["1"],
                "Ordered": ["3"],
            }
        )
        stock = build_template_stock_on_hand(
            inventory,
            ["partCode", "warehouseCode", "inventoryType", "quantityAllocated", "quantityOnHand", "quantityInbound", "quantityOutbound", "uniqueId"],
        )
        self.assertEqual(stock.loc[0, "partCode"], "A1")
        self.assertEqual(stock.loc[0, "quantityOnHand"], 2)
        self.assertEqual(stock.loc[0, "inventoryType"], "")

    def test_template_parts_leaves_unproven_primary_fields_blank(self) -> None:
        parts = pd.DataFrame({"ItemNo": ["A1"], "SPLMaster": ["SPL1"], "ItemDescription": ["Part A"], "DisplayItemNo": ["A1"]})
        out = build_template_parts(parts, ["SPLMaster", "PartNumber", "isPrimary", "primaryPartNumber", "description"])
        self.assertEqual(out.loc[0, "SPLMaster"], "")
        self.assertEqual(out.loc[0, "isPrimary"], "")
        self.assertEqual(out.loc[0, "primaryPartNumber"], "")

    def test_template_parts_populates_spl_master_from_reference_masters(self) -> None:
        parts = pd.DataFrame({"ItemNo": ["34076947"], "ItemDescription": ["Part A"]})
        masters = pd.DataFrame({"SPL Master": ["SPL8000000"], "Items linked": ["34076947;38049457"]})
        out = build_template_parts(parts, ["SPLMaster", "PartNumber", "description"], masters=masters)
        self.assertEqual(out.loc[0, "SPLMaster"], "SPL8000000")

    def test_template_parts_populates_primary_from_spi_main_alternative(self) -> None:
        parts = pd.DataFrame(
            {
                "ItemNo": ["34076947", "38049457"],
                "SPLMaster": ["SPL1", "SPL1"],
                "ItemDescription": ["Main", "Alt"],
            }
        )
        spi = pd.DataFrame(
            {
                "Material": ["000000000034076947", "000000000038049457"],
                "PartNumber": ["34076947", "38049457"],
                "Main alternative par": ["000000000034076947", "000000000034076947"],
            }
        )
        out = build_template_parts(parts, ["SPLMaster", "PartNumber", "isPrimary", "primaryPartNumber", "description"], spi)
        self.assertEqual(out.loc[0, "isPrimary"], "True")
        self.assertEqual(out.loc[1, "isPrimary"], "False")
        self.assertEqual(out.loc[1, "primaryPartNumber"], "34076947")

    def test_template_parts_usage_uses_negative_dn_rows_only(self) -> None:
        usage = pd.DataFrame(
            {
                "Item No.": ["A1", "", "B2", "C3"],
                "Description": ["Part A", "", "Part B", "Part C"],
                "Posting Date": ["01/02/26", "02/02/26", "03/02/26", "04/02/26"],
                "Document": ["DN 1", "DN 2", "IM 1", "DN 3"],
                "Whse": ["WH1", "WH1", "WH2", "WH3"],
                "Quantity": ["-2", "-1", "-3", "4"],
            }
        )
        out = build_template_parts_usage(
            usage,
            ["orderNumber", "partCode", "Warehouse", "quantityUsed", "partsUsedDateTime", "orderType"],
        )
        self.assertEqual(list(out["orderNumber"]), ["DN 1", "DN 2"])
        self.assertEqual(list(out["partCode"]), ["A1", "A1"])
        self.assertEqual(list(out["quantityUsed"]), [2, 1])
        self.assertEqual(out.loc[0, "partsUsedDateTime"], "2026-02-01")

    def test_validation_flags_missing_confirmed_csv_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_dir = root / "onboarding_csvs"
            template_dir = root / "investigation_templates"
            csv_dir.mkdir()
            template_dir.mkdir()
            for object_name in CONFIRMED_OUTPUT_OBJECTS:
                pd.DataFrame({"wrong": []}).to_csv(csv_dir / f"{object_name}.csv", index=False)
            for object_name, columns in PENDING_OUTPUT_OBJECTS.items():
                pd.DataFrame(columns=columns).to_csv(template_dir / f"{object_name}.csv", index=False)

            result = validate_outputs(csv_dir, template_dir)

        self.assertIn("FAIL", set(result["Status"]))
        self.assertIn("PENDING", set(result["Status"]))


if __name__ == "__main__":
    unittest.main()
