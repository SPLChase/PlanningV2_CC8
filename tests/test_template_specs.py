from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from planning_v2.template_specs import template_columns


class TemplateSpecTests(unittest.TestCase):
    def test_individual_template_workbook_overrides_raw_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            samples = Path(tmp)
            raw = Workbook()
            ws = raw.active
            ws.title = "WarehouseStockOnHand_Template"
            ws.append(["FieldName", "FieldType", "Examples", "Notes"])
            ws.append(["partCode", "String", "", ""])
            raw.save(samples / "Templates raw.xlsx")

            individual = Workbook()
            sheet = individual.active
            sheet.append(["partNumber", "warehouseCode"])
            sheet.append(["A1", "WH1"])
            individual.save(samples / "Warehouse Stock on Hand.xlsx")

            columns = template_columns(samples)

        self.assertEqual(columns["WarehouseStockOnHand"], ["partNumber", "warehouseCode"])


if __name__ == "__main__":
    unittest.main()
