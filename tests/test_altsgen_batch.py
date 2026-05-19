from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planning_v2.altsgen_batch import _refresh_parts_csv_part_types


class AltsgenBatchTests(unittest.TestCase):
    def test_refresh_parts_csv_part_types_updates_only_confirmed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Parts.csv"
            pd.DataFrame(
                {
                    "PartNumber": ["A1", "B2", "C3"],
                    "partType": ["", "existing", ""],
                }
            ).to_csv(path, index=False, encoding="utf-8-sig")
            evidence = pd.DataFrame(
                {
                    "PartNumber": ["A1", "B2", "C3"],
                    "status": ["ok", "ok", "error"],
                    "partType": ["hdd_sas_2_5_sff", "memory_module", "battery"],
                }
            )

            changed = _refresh_parts_csv_part_types(path, evidence)
            refreshed = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")

        self.assertEqual(changed, 2)
        self.assertEqual(list(refreshed["partType"]), ["hdd_sas_2_5_sff", "memory_module", ""])


if __name__ == "__main__":
    unittest.main()
