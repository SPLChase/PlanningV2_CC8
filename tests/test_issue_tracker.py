from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planning_v2.issue_tracker import (
    issue_tracker_evidence_rows,
    po_key,
    purchase_order_reconciliation,
    purchase_order_ticket_matches,
    read_issue_tracker,
)


class IssueTrackerTests(unittest.TestCase):
    def test_po_key_allows_explicit_single_po_token_only(self) -> None:
        self.assertEqual(po_key("PO 00012345"), "12345")
        self.assertEqual(po_key("12345"), "12345")
        self.assertEqual(po_key("26PO000031.pdf"), "26PO000031")
        self.assertEqual(po_key("PO 12345 and 67890"), "")

    def test_purchase_order_match_allows_same_spl_master_alternative(self) -> None:
        purchase_orders = pd.DataFrame(
            {
                "PurchaseOrderNumber": ["50001"],
                "PartNumber": ["ALT2"],
            }
        )
        issue_tracker = pd.DataFrame(
            {
                "PurchaseOrderKey": ["50001"],
                "Part Nr": ["MAIN1"],
                "PartKey": ["MAIN1"],
                "DispatchPartNo": [""],
                "DispatchPartKey": [""],
                "Call Number": ["777"],
                "MSConvoID": ["thread-1"],
                "ReplenishStatus": ["Approved"],
            }
        )
        masters = pd.DataFrame({"SPL Master": ["SPL1"], "Items linked": ["MAIN1;ALT2"]})

        matches = purchase_order_ticket_matches(purchase_orders, issue_tracker, masters)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches.loc[0, "MatchType"], "Exact PO + same SPL Master")
        self.assertEqual(matches.loc[0, "SapSPLMaster"], "SPL1")

    def test_issue_tracker_evidence_flags_missing_master(self) -> None:
        issue_tracker = pd.DataFrame(
            {
                "Call Number": ["777"],
                "MSConvoID": ["thread-1"],
                "Created": ["2026-05-01"],
                "Status": ["Open"],
                "Customer": ["Customer"],
                "CustomerNormalized": ["Other"],
                "Part Nr": ["UNKNOWN"],
                "PartKey": ["UNKNOWN"],
                "DispatchPartNo": [""],
                "DispatchPartKey": [""],
                "SPLMaster": [""],
                "Quantity": ["1"],
                "PurchaseOrder": [""],
                "ReplenishStatus": [""],
                "WorkOrder": [""],
                "Serial Nr": [""],
                "DeliveryCity": [""],
                "DeliveryAddress": [""],
            }
        )
        evidence = issue_tracker_evidence_rows(issue_tracker, pd.DataFrame())

        self.assertEqual(evidence.loc[0, "EvidenceStatus"], "Needs SPL Master mapping")

    def test_purchase_order_reconciliation_flags_missing_sap_po_number(self) -> None:
        purchase_orders = pd.DataFrame({"PurchaseOrderNumber": ["50001"], "PartNumber": ["MAIN1"]})
        issue_tracker = pd.DataFrame(
            {
                "PurchaseOrderKey": ["26PO000031"],
                "Part Nr": ["MAIN1"],
                "DispatchPartNo": [""],
                "SPLMaster": ["SPL1"],
                "Call Number": ["777"],
                "MSConvoID": ["thread-1"],
                "ReplenishStatus": ["Ordered"],
            }
        )

        reconciliation = purchase_order_reconciliation(purchase_orders, issue_tracker, pd.DataFrame())

        self.assertEqual(reconciliation.loc[0, "MatchStatus"], "No matching SAP PO number")

    def test_read_issue_tracker_skips_sharepoint_schema_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CoCre8 Issue Tracker V3.2 (2).csv"
            path.write_text(
                'ListSchema={"schemaXmlList":["<Field DisplayName=\\"Customer\\" />"]}\n'
                '"Call Number","Created","Status","Customer","Part Nr","Serial Nr"\n'
                '"74782263 - 539904","2026-05-27T05:52:54Z","DeliveryNote","Department of Justice","DB.VYQ11.002","SER123"\n',
                encoding="utf-8",
            )

            out = read_issue_tracker(path)

        self.assertEqual(list(out["Call Number"]), ["74782263 - 539904"])
        self.assertEqual(list(out["Status"]), ["DeliveryNote"])
        self.assertEqual(list(out["Serial Nr"]), ["SER123"])


if __name__ == "__main__":
    unittest.main()
