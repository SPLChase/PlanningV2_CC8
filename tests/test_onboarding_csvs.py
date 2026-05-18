from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planning_v2.onboarding_csvs import (
    build_template_part_cost,
    build_template_parts,
    build_template_parts_usage,
    build_template_parts_usage_from_issue_tracker,
    build_template_purchase_orders,
    build_template_warehouses,
    build_template_stock_on_hand,
    build_template_vendors,
    filter_active_warehouses,
    build_stock_detail,
    validate_outputs,
)
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
        masters = pd.DataFrame({"SPL Master": ["SPL1"], "Items linked": ["A1"]})
        stock = build_template_stock_on_hand(
            inventory,
            ["partNumber", "warehouseCode", "inventoryType", "quantityAllocated", "quantityOnHand", "quantityInbound", "quantityOutbound", "uniqueId"],
            masters,
        )
        self.assertEqual(stock.loc[0, "partNumber"], "A1")
        self.assertEqual(stock.loc[0, "quantityOnHand"], 2)
        self.assertEqual(stock.loc[0, "inventoryType"], "")
        self.assertNotEqual(stock.loc[0, "uniqueId"], "")

    def test_template_stock_on_hand_populates_canonical_part_code(self) -> None:
        inventory = pd.DataFrame({"ItemNo": ["A1"], "WarehouseCode": ["WH1"], "OnHand": ["2"]})
        stock = build_template_stock_on_hand(inventory, ["partCode", "warehouseCode", "quantityOnHand"])
        self.assertEqual(stock.loc[0, "partCode"], "A1")
        self.assertEqual(stock.loc[0, "warehouseCode"], "WH1")
        self.assertEqual(stock.loc[0, "quantityOnHand"], 2)

    def test_template_vendors_maps_po_vendor_lookup_fields(self) -> None:
        purchase_orders = pd.DataFrame(
            {
                "VendorId": ["V001", "V001", "V002"],
                "VendorName": ["Fujitsu", "Fujitsu Duplicate", "Inactive Vendor"],
                "VendorIsActive": ["Y", "Y", "N"],
            }
        )
        vendors = build_template_vendors(purchase_orders, ["vendorId", "Description", "isActive"])
        self.assertEqual(list(vendors["vendorId"]), ["V001", "V002"])
        self.assertEqual(vendors.loc[0, "Description"], "Fujitsu")
        self.assertEqual(vendors.loc[1, "isActive"], "N")

    def test_warehouse_activity_helper_can_identify_inactive_without_deleting_fill_rows(self) -> None:
        inventory = pd.DataFrame(
            {
                "ItemNo": ["A1", "B2"],
                "WarehouseCode": ["ACTIVE", "EMPTY"],
                "OnHand": [1, 0],
                "IsCommited": [0, 0],
                "OnOrder": [0, 0],
            }
        )
        warehouses = pd.DataFrame(
            {
                "WarehouseCode": ["ACTIVE", "EMPTY"],
                "WarehouseName": ["Active", "Empty"],
            }
        )
        movements = pd.DataFrame({"WarehouseCode": ["ACTIVE"], "MovementCount": [2]})

        filtered_inventory, filtered_warehouses = filter_active_warehouses(inventory, warehouses, movements)

        self.assertEqual(list(filtered_inventory["WarehouseCode"]), ["ACTIVE"])
        self.assertEqual(list(filtered_warehouses["WarehouseCode"]), ["ACTIVE"])

    def test_template_warehouses_maps_manual_answers_and_inverts_obsolete_status(self) -> None:
        warehouses = pd.DataFrame({"WarehouseCode": ["WH1", "WH2"], "WarehouseName": ["Main", "Old"]})
        manual = pd.DataFrame(
            {
                "warehouseId": ["WH1", "WH2"],
                "addressId": ["WH1", "WH2"],
                "returnWarehouseId": ["RET", "RET"],
                "supplyWarehouseId": ["MAIN", ""],
                "warehouseTypeId": ["CUSTOMER", "VIRTUAL"],
                "isReplenishable": ["TRUE", "FALSE"],
                "isBranchStockable": ["TRUE", "FALSE"],
                "isRemote": ["FALSE", "TRUE"],
                "isObsolete": ["N", "Y"],
            }
        )

        out = build_template_warehouses(
            warehouses,
            [
                "warehouseId",
                "addressId",
                "returnWarehouseId",
                "supplyWarehouseId",
                "warehouseTypeId",
                "warehouseDescription",
                "isReplenishable",
                "isBranchStockable",
                "isRemote",
                "warehouseStatusId",
            ],
            manual,
        )

        self.assertEqual(out.loc[0, "addressId"], "WH1")
        self.assertEqual(out.loc[0, "warehouseStatusId"], "Y")
        self.assertEqual(out.loc[1, "warehouseStatusId"], "N")

    def test_template_parts_defaults_missing_main_alternative_to_primary(self) -> None:
        parts = pd.DataFrame({"ItemNo": ["A1"], "SPLMaster": ["SPL1"], "ItemDescription": ["Part A"], "DisplayItemNo": ["A1"]})
        out = build_template_parts(parts, ["SPLMaster", "PartNumber", "isPrimary", "primaryPartNumber", "description"])
        self.assertEqual(out.loc[0, "SPLMaster"], "")
        self.assertEqual(out.loc[0, "isPrimary"], "True")
        self.assertEqual(out.loc[0, "primaryPartNumber"], "A1")

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

    def test_template_parts_applies_confirmed_co_cre8_defaults(self) -> None:
        parts = pd.DataFrame(
            {
                "ItemNo": ["A1", "B2"],
                "ItemDescription": ["BBU pack", ""],
                "DisplayDescription": ["", ""],
            }
        )

        out = build_template_parts(
            parts,
            [
                "PartNumber",
                "description",
                "isBootStockable",
                "isBranchStockable",
                "isObsolete",
                "isExcludeFromReplenishment",
                "purchaseLeadTimeDays",
            ],
        )

        self.assertEqual(out.loc[0, "isBootStockable"], "N")
        self.assertEqual(out.loc[0, "isBranchStockable"], "Y")
        self.assertEqual(out.loc[0, "isObsolete"], "N")
        self.assertEqual(out.loc[0, "isExcludeFromReplenishment"], "N")
        self.assertEqual(out.loc[0, "purchaseLeadTimeDays"], 180)
        self.assertEqual(out.loc[1, "description"], "null")
        self.assertEqual(out.loc[1, "purchaseLeadTimeDays"], 3)

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

    def test_template_parts_usage_can_include_spl_master_column(self) -> None:
        usage = pd.DataFrame(
            {
                "Item No.": ["A1"],
                "Description": ["Part A"],
                "Posting Date": ["01/02/26"],
                "Document": ["DN 1"],
                "Whse": ["WH1"],
                "Quantity": ["-2"],
            }
        )
        masters = pd.DataFrame({"SPL Master": ["SPL1"], "Items linked": ["A1;B2"]})

        out = build_template_parts_usage(usage, ["orderNumber", "partCode", "Master"], masters)

        self.assertEqual(out.loc[0, "Master"], "SPL1")

    def test_template_parts_usage_from_issue_tracker_can_build_review_rows(self) -> None:
        tracker = pd.DataFrame(
            {
                "Call Number": ["71919593"],
                "Created": ["2026/01/20"],
                "Status": ["Open"],
                "Customer": ["WCED"],
                "CustomerNormalized": [""],
                "Part Nr": ["38064536"],
                "DispatchPartNo": ["38066709"],
                "Quantity": ["1"],
                "Serial Nr": ["EWAA007343"],
                "DispatchWarehouse": ["FUJ CT"],
                "SPLMaster": [""],
            }
        )
        masters = pd.DataFrame({"SPL Master": ["SPL1"], "Items linked": ["38066709;38064536"]})

        out = build_template_parts_usage_from_issue_tracker(
            tracker,
            [
                "orderNumber",
                "requestId",
                "customerCompanyCode",
                "orderStartDatetime",
                "orderStatus",
                "partCode",
                "serialNumber",
                "quantityUsed",
                "partsUsedDateTime",
                "Warehouse",
                "deviceSerialNumber",
                "Master",
            ],
            masters,
        )

        self.assertEqual(out.loc[0, "orderNumber"], "71919593")
        self.assertEqual(out.loc[0, "requestId"], "71919593")
        self.assertEqual(out.loc[0, "customerCompanyCode"], "WCED")
        self.assertEqual(out.loc[0, "partCode"], "38066709")
        self.assertEqual(out.loc[0, "serialNumber"], "EWAA007343")
        self.assertEqual(out.loc[0, "deviceSerialNumber"], "EWAA007343")
        self.assertEqual(out.loc[0, "Master"], "SPL1")

    def test_template_parts_usage_from_issue_tracker_requires_call_number_for_review_rows(self) -> None:
        tracker = pd.DataFrame(
            {
                "Call Number": ["", "71919593"],
                "Created": ["2026/01/20", "2026/01/20"],
                "Status": ["Open", "Open"],
                "Customer": ["WCED", "WCED"],
                "Part Nr": ["99999999", "38064536"],
                "DispatchPartNo": ["", ""],
                "Quantity": ["1", "1"],
            }
        )

        out = build_template_parts_usage_from_issue_tracker(tracker, ["orderNumber", "partCode"])

        self.assertEqual(len(out), 1)
        self.assertEqual(out.loc[0, "orderNumber"], "71919593")

    def test_template_parts_usage_prefers_stock_audit_actual_usage_over_helpdesk(self) -> None:
        usage = pd.DataFrame(
            {
                "Item No.": ["ACTUALALT"],
                "Description": ["Actual Part"],
                "Posting Date": ["01/02/26"],
                "Document": ["DN 1"],
                "Whse": ["WH1"],
                "Quantity": ["-1"],
            }
        )
        tracker = pd.DataFrame(
            {
                "Call Number": ["71919593"],
                "Created": ["2026/01/20"],
                "Part Nr": ["REQUESTED"],
                "Quantity": ["1"],
            }
        )
        masters = pd.DataFrame({"SPL Master": ["SPL1"], "Items linked": ["ACTUALALT;REQUESTED"]})

        out = build_template_parts_usage(usage, ["orderNumber", "partCode", "Master"], masters, tracker)

        self.assertEqual(out.loc[0, "orderNumber"], "DN 1")
        self.assertEqual(out.loc[0, "partCode"], "ACTUALALT")
        self.assertEqual(out.loc[0, "Master"], "SPL1")

    def test_template_parts_usage_enriches_sap_delivery_note_context(self) -> None:
        usage = pd.DataFrame(
            {
                "DeliveryNoteNumber": ["50006351"],
                "DocDate": ["20251017"],
                "CustomerRefNumber": ["67548724"],
                "Comments": [
                    "Part nr 38062982 [HD SAS].\rCall Nr 67548724\rCustomer: Massmart\rWO 995005378555\rSerial number: YM6D008491\rSLA: TMS 8HRS Rec"
                ],
                "ItemNo": ["38062982"],
                "WarehouseCode": ["FUJITSU"],
                "Quantity": [1],
            }
        )
        masters = pd.DataFrame({"SPL Master": ["SPL1"], "Items linked": ["38062982;ALT2"]})

        out = build_template_parts_usage(
            usage,
            [
                "orderNumber",
                "requestId",
                "customerCompanyCode",
                "orderStartDatetime",
                "orderStatus",
                "partCode",
                "serialNumber",
                "quantityUsed",
                "partsUsedDateTime",
                "Warehouse",
                "deviceSerialNumber",
                "Master",
            ],
            masters,
            pd.DataFrame({"Call Number": ["67548724"], "Created": ["2025/10/15"], "Status": ["Closed"]}),
        )

        self.assertEqual(out.loc[0, "orderNumber"], "67548724")
        self.assertEqual(out.loc[0, "requestId"], "67548724")
        self.assertEqual(out.loc[0, "customerCompanyCode"], "Massmart")
        self.assertEqual(out.loc[0, "orderStartDatetime"], "2025-10-15")
        self.assertEqual(out.loc[0, "orderStatus"], "Closed")
        self.assertEqual(out.loc[0, "partCode"], "38062982")
        self.assertEqual(out.loc[0, "serialNumber"], "YM6D008491")
        self.assertEqual(out.loc[0, "deviceSerialNumber"], "YM6D008491")
        self.assertEqual(out.loc[0, "quantityUsed"], 1)
        self.assertEqual(out.loc[0, "partsUsedDateTime"], "2025-10-17")
        self.assertEqual(out.loc[0, "Master"], "SPL1")

    def test_template_purchase_orders_maps_sap_po_lines_and_receipts(self) -> None:
        source = pd.DataFrame(
            {
                "PurchaseOrderNumber": [50002190, 50002189, 50002188],
                "SapInternalPurchaseOrderNumber": [2190, 2189, 2188],
                "DocStatus": ["O", "C", "O"],
                "Canceled": ["N", "N", "Y"],
                "CreationDateTime": ["20260507", "20260506", "20260505"],
                "ApprovalDateTime": ["20260507", "20260506", "20260505"],
                "ToWarehouseId": ["FUJITSU", "FUJITSU", "ACER"],
                "VendorId": ["FSC007s", "FSC007s", "VEND1"],
                "PartNumber": ["0000123", "ABC", "XYZ"],
                "Quantity": [1, 3, 4],
                "LineCost": [10.5, 20, 30],
                "QuantityReceived": [0, 3, 0],
                "ReceivedDateTime": ["", "20260507", ""],
            }
        )
        out = build_template_purchase_orders(
            source,
            [
                "purchaseOrderNumber",
                "purchaseOrderStatus",
                "creationDateTime",
                "approvalDateTime",
                "toWarehouseId",
                "vendorId",
                "partNumber",
                "quantity",
                "lineCost",
                "quantityReceived",
                "receivedDateTime",
                "demandStatus",
            ],
        )
        self.assertEqual(list(out["purchaseOrderStatus"]), ["Accepted", "Fulfilled", "Cancelled"])
        self.assertEqual(out.loc[0, "creationDateTime"], "2026-05-07")
        self.assertEqual(out.loc[0, "partNumber"], "123")
        self.assertEqual(out.loc[1, "receivedDateTime"], "2026-05-07")
        self.assertEqual(out.loc[0, "demandStatus"], "")

    def test_template_purchase_orders_uses_external_sap_po_number_for_matching(self) -> None:
        source = pd.DataFrame(
            {
                "PurchaseOrderNumber": ["26PO000031"],
                "SapInternalPurchaseOrderNumber": ["50002190"],
                "DocStatus": ["O"],
                "Canceled": ["N"],
                "PartNumber": ["38047180"],
                "Quantity": [1],
                "QuantityReceived": [0],
            }
        )
        issue_tracker = pd.DataFrame(
            {
                "PurchaseOrderKey": ["26PO000031"],
                "Part Nr": ["38047180"],
                "PartKey": ["38047180"],
                "DispatchPartNo": [""],
                "DispatchPartKey": [""],
                "Call Number": ["71976661"],
                "MSConvoID": ["thread-1"],
                "ReplenishStatus": ["Ordered"],
            }
        )

        out = build_template_purchase_orders(
            source,
            ["purchaseOrderNumber", "partNumber", "demandStatus"],
            None,
            issue_tracker,
        )

        self.assertEqual(out.loc[0, "purchaseOrderNumber"], "26PO000031")
        self.assertEqual(out.loc[0, "demandStatus"], "Ordered")

    def test_template_purchase_orders_does_not_fallback_to_internal_sap_number(self) -> None:
        source = pd.DataFrame(
            {
                "PurchaseOrderNumber": [""],
                "SapInternalPurchaseOrderNumber": ["50002190"],
                "PartNumber": ["38047180"],
            }
        )

        out = build_template_purchase_orders(source, ["purchaseOrderNumber", "partNumber"])

        self.assertEqual(out.loc[0, "purchaseOrderNumber"], "")

    def test_template_purchase_orders_uses_spi_cocre8_cost_for_line_cost(self) -> None:
        source = pd.DataFrame(
            {
                "PurchaseOrderNumber": ["26PO000031", "26PO000032"],
                "PartNumber": ["38047180", "OLDPART"],
                "LineCost": [999999, 888888],
            }
        )
        spi = pd.DataFrame(
            {
                "Material": ["000000000038047180", "", "OLDPART"],
                "PartNumber": ["38047180", "OLDPART", "OLDPART"],
                "ListPrice": ["100.00", "80.00", "70.00"],
            }
        )

        out = build_template_purchase_orders(source, ["purchaseOrderNumber", "partNumber", "lineCost"], spi=spi)

        self.assertEqual(out.loc[0, "lineCost"], 72.0)
        self.assertEqual(out.loc[1, "lineCost"], 57.6)

    def test_template_purchase_orders_enriches_demand_status_with_strict_ticket_evidence(self) -> None:
        source = pd.DataFrame(
            {
                "PurchaseOrderNumber": ["50001"],
                "DocStatus": ["O"],
                "Canceled": ["N"],
                "PartNumber": ["ALT2"],
                "Quantity": [1],
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

        out = build_template_purchase_orders(source, ["purchaseOrderNumber", "partNumber", "demandStatus"], masters, issue_tracker)

        self.assertEqual(out.loc[0, "demandStatus"], "Approved")

    def test_template_part_cost_uses_last_spi_price_as_of_last_po_month_and_weighted_average(self) -> None:
        parts = pd.DataFrame(
            {
                "ItemNo": ["A1", "B2"],
                "SPLMaster": ["SPL1", "SPL2"],
            }
        )
        purchase_orders = pd.DataFrame(
            {
                "PartNumber": ["A1", "A1", "B2"],
                "ApprovalDateTime": ["20260110", "20260320", ""],
                "Quantity": [1, 3, 2],
            }
        )
        spi = pd.DataFrame(
            {
                "PartNumber": ["A1", "A1", "B2"],
                "Material": ["", "", ""],
                "ListPrice": ["100", "200", "50"],
                "SourceDate": ["2026-01-01", "2026-03-01", "2026-02-01"],
                "SourceFile": ["Jan", "Mar", "Feb"],
            }
        )

        out = build_template_part_cost(
            parts,
            purchase_orders,
            ["partCode", "cost", "currencyCode", "averageCost", "averageRepairCost"],
            spi,
        )

        a1 = out[out["partCode"].eq("A1")].iloc[0]
        b2 = out[out["partCode"].eq("B2")].iloc[0]
        self.assertEqual(a1["cost"], 144.0)
        self.assertEqual(a1["averageCost"], 126.0)
        self.assertEqual(a1["currencyCode"], "EUR")
        self.assertEqual(a1["averageRepairCost"], "")
        self.assertEqual(b2["cost"], 36.0)

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
