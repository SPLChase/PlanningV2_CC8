from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planning_v2.onboarding_csvs import build_template_parts, build_template_stock_on_hand, build_stock_detail, validate_outputs
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
        self.assertEqual(out.loc[0, "SPLMaster"], "SPL1")
        self.assertEqual(out.loc[0, "isPrimary"], "")
        self.assertEqual(out.loc[0, "primaryPartNumber"], "")

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
