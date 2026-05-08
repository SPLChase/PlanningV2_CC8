from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from planning_v2.config import PlanningConfig
from planning_v2.sap_queries import (
    build_open_po_sql,
    build_purchase_order_lines_sql,
    build_purchase_order_receipts_sql,
    build_stock_sql,
    build_template_stock_on_hand_sql,
)


def _cfg() -> PlanningConfig:
    return PlanningConfig(
        raw_dir=Path("data/raw"),
        stage_dir=Path("data/stage"),
        output_dir=Path("data/output"),
        reference_dir=Path("Reference"),
        samples_dir=Path("Samples"),
        cc8_source_root=Path("C:/dev/cc8"),
        exco_source_dir=Path("C:/dev/cc8/Exco/source_data"),
        exco_output_dir=Path("C:/dev/cc8/Exco/output/csv"),
        minstock3_dir=Path("C:/dev/cc8/MinStock3"),
        sap_url="https://sap.example/b1s/v1",
        sap_company="CC8",
        sap_user="user",
        sap_password="pass",
        sap_verify_ssl=False,
        sql_query_code="CC8_PLANNING_V2_STOCK_RAW",
        odata_max_page_size=500,
        group_min=105,
        group_max=106,
    )


class SapQueryTests(unittest.TestCase):
    def test_stock_query_matches_minstock3_core_tables_and_columns(self) -> None:
        sql = build_stock_sql(_cfg())
        for token in ['OITW', 'OITM', '"ItemCode"', '"WhsCode"', '"OnHand"', '"OnOrder"', '"MinStock"']:
            self.assertIn(token, sql)
        self.assertIn("BETWEEN 105 AND 106", sql)

    def test_open_po_query_uses_opor_por1_and_90_day_cutoff(self) -> None:
        sql = build_open_po_sql(_cfg(), today=date(2026, 5, 7))
        for token in ["POR1", "OPOR", '"OpenQty"', "2026-02-06"]:
            self.assertIn(token, sql)

    def test_template_stock_query_uses_live_exco_stock_metrics(self) -> None:
        sql = build_template_stock_on_hand_sql()
        for token in ['OITW', 'OITM', '"OnHand"', '"IsCommited"', '"OnOrder"', '"MinStock"', '"MaxStock"', '"AvgPrice"']:
            self.assertIn(token, sql)

    def test_purchase_order_queries_use_po_and_grpo_tables(self) -> None:
        line_sql = build_purchase_order_lines_sql()
        receipt_sql = build_purchase_order_receipts_sql()
        for token in ["OPOR", "POR1", '"DocNum"', '"CardCode"', '"LineTotal"']:
            self.assertIn(token, line_sql)
        for token in ["OPDN", "PDN1", '"BaseType" = 22', "SUM", "MAX"]:
            self.assertIn(token, receipt_sql)


if __name__ == "__main__":
    unittest.main()
