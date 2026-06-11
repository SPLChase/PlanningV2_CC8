from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planning_v2.onboarding_csvs import (
    build_template_part_cost,
    build_template_part_types,
    build_template_parts,
    build_template_parts_usage,
    build_template_parts_usage_from_issue_tracker,
    build_template_outputs,
    build_template_purchase_orders,
    build_template_service_orders,
    _cost_category,
    build_template_addresses,
    build_template_customers,
    build_template_warehouses,
    build_template_warehouse_exclusions,
    build_template_stock_on_hand,
    build_template_vendors,
    build_template_inventory_transfers,
    filter_active_warehouses,
    filter_excluded_warehouse_sources,
    build_stock_detail,
    parse_customer_sla,
    validate_template_outputs,
    validate_outputs,
)
from planning_v2.schemas import CONFIRMED_OUTPUT_OBJECTS, PENDING_OUTPUT_OBJECTS


class OnboardingCsvTests(unittest.TestCase):
    def test_template_customers_uses_helpdesk_customer_names_with_generated_ids(self) -> None:
        issue_tracker = pd.DataFrame(
            {
                "Customer": ["Sanlam", "SANLAM", "Massmart", ""],
            }
        )

        out = build_template_customers(
            pd.DataFrame(),
            ["customerId", "customerName", "customerGroupId", "assignAnySkill", "isActive"],
            issue_tracker,
        )

        self.assertEqual(list(out["customerName"]), ["Massmart", "Sanlam"])
        self.assertEqual(list(out["customerId"]), ["CC8CUST-3CC274F6", "CC8CUST-BD0FB2C0"])
        self.assertEqual(set(out["assignAnySkill"]), {"Y"})
        self.assertEqual(set(out["isActive"]), {"Y"})

    def test_template_customers_populates_sla_hours_from_helpdesk(self) -> None:
        issue_tracker = pd.DataFrame(
            {
                "Customer": ["Massmart", "Massmart", "WCED", "Tracker"],
                "SLA": ["8 Hours Recovery 24x7", "8h recovery, 24x7", "NBD Response 9x5", "4h Response, 24 x 7"],
            }
        )

        out = build_template_customers(
            pd.DataFrame(),
            ["customerName", "stdResponseTime", "stdRepairTime"],
            issue_tracker,
        )

        by_customer = out.set_index("customerName").to_dict("index")
        self.assertEqual(by_customer["Massmart"]["stdResponseTime"], "8")
        self.assertEqual(by_customer["Massmart"]["stdRepairTime"], "8")
        self.assertEqual(by_customer["WCED"]["stdResponseTime"], "16")
        self.assertEqual(by_customer["WCED"]["stdRepairTime"], "")
        self.assertEqual(by_customer["Tracker"]["stdResponseTime"], "4")
        self.assertEqual(by_customer["Tracker"]["stdRepairTime"], "")

    def test_parse_customer_sla_is_conservative_for_untyped_hours(self) -> None:
        self.assertEqual(parse_customer_sla("4 hour")["stdResponseTime"], "")
        self.assertEqual(parse_customer_sla("Low")["stdRepairTime"], "")

    def test_template_addresses_uses_sap_ship_to_and_generated_customer_id(self) -> None:
        usage = pd.DataFrame(
            {
                "Comments": ["Customer: Massmart\rSLA: 8h recovery, 24x7"],
                "CustomerRefNumber": ["77544562"],
                "ShipToCode": ["CoCre8 Technology Solutions"],
                "ShipToAddress": ["Kelvin to collect\r\r\rSOUTH AFRICA"],
                "WarehouseCode": ["FUJMSM C"],
            }
        )
        warehouse_locations = pd.DataFrame(
            {
                "WarehouseCode": ["FUJMSM C"],
                "Location": ["CPT"],
                "WarehouseName": ["MASSMART - CPT - SPL"],
            }
        )

        out = build_template_addresses(
            usage,
            [
                "externalAddressId",
                "customerExternalId",
                "addressLine1",
                "addressLine2",
                "addressLine3",
                "city",
                "stateProvince",
                "countryCode",
                "latitude",
                "longitude",
                "timeZone",
                "nodeId",
            ],
            warehouse_locations,
        )

        customer_rows = out[out["customerExternalId"].eq("CC8CUST-3CC274F6")].reset_index(drop=True)
        self.assertEqual(len(customer_rows), 1)
        self.assertTrue(customer_rows.loc[0, "externalAddressId"].startswith("CC8ADDR-"))
        self.assertEqual(customer_rows.loc[0, "addressLine1"], "Kelvin to collect")
        self.assertEqual(customer_rows.loc[0, "addressLine2"], "SOUTH AFRICA")
        self.assertEqual(customer_rows.loc[0, "addressLine3"], "")
        self.assertEqual(customer_rows.loc[0, "city"], "Cape Town")
        self.assertEqual(customer_rows.loc[0, "stateProvince"], "Western Cape")
        self.assertEqual(customer_rows.loc[0, "countryCode"], "ZA")
        self.assertEqual(customer_rows.loc[0, "timeZone"], "South Africa Standard Time")
        self.assertEqual(customer_rows.loc[0, "nodeId"], "")

    def test_template_addresses_marks_rssc_as_swaziland(self) -> None:
        usage = pd.DataFrame(
            {
                "Comments": ["Customer: Royal Eswatini Sugar Corporation"],
                "CustomerRefNumber": ["S_1"],
                "ShipToCode": [""],
                "ShipToAddress": ["Royal Eswatini Sugar Corporation (RSSC)\rSimunye - New IT Offices\rEswatini"],
                "WarehouseCode": ["FUJ RSSC"],
            }
        )

        out = build_template_addresses(usage, ["customerExternalId", "city", "stateProvince", "countryCode", "latitude", "longitude"])

        self.assertEqual(out.loc[0, "city"], "Simunye")
        self.assertEqual(out.loc[0, "stateProvince"], "Lubombo")
        self.assertEqual(out.loc[0, "countryCode"], "SZ")

    def test_template_addresses_splits_address_lines_one_two_three(self) -> None:
        usage = pd.DataFrame(
            {
                "Comments": ["Customer: Massmart"],
                "CustomerRefNumber": ["77544562"],
                "ShipToCode": [""],
                "ShipToAddress": ["Line 1\rLine 2\rLine 3\rLine 4"],
                "WarehouseCode": ["FUJMSM C"],
            }
        )

        out = build_template_addresses(usage, ["addressLine1", "addressLine2", "addressLine3"])

        self.assertEqual(out.loc[0, "addressLine1"], "Line 1")
        self.assertEqual(out.loc[0, "addressLine2"], "Line 2")
        self.assertEqual(out.loc[0, "addressLine3"], "Line 3 | Line 4")

    def test_template_addresses_includes_physical_warehouse_addresses(self) -> None:
        out = build_template_addresses(
            pd.DataFrame(),
            [
                "externalAddressId",
                "customerExternalId",
                "addressLine1",
                "addressLine2",
                "addressLine3",
                "city",
                "stateProvince",
                "countryCode",
                "latitude",
                "longitude",
                "timeZone",
            ],
        )
        by_id = out.set_index("externalAddressId")

        self.assertEqual(by_id.loc["FUJITSU", "addressLine1"], "5 Star Junction")
        self.assertEqual(by_id.loc["FUJITSU", "addressLine2"], "Cnr Beyers Naude & Juice St")
        self.assertEqual(by_id.loc["FUJITSU", "city"], "Johannesburg")
        self.assertEqual(by_id.loc["FUJITSU", "latitude"], "-26.0739306")
        self.assertEqual(by_id.loc["FUJ CT", "addressLine1"], "Unit L4, Centurion Business Park")
        self.assertEqual(by_id.loc["FUJ CT", "city"], "Cape Town")
        self.assertEqual(by_id.loc["FUJ CT", "longitude"], "18.5079583")

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
        self.assertEqual(stock.loc[0, "Ws Part"], "A1")
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
        vendors = build_template_vendors(purchase_orders, ["vendorId", "vendorName", "Description", "isActive"])
        self.assertEqual(list(vendors["vendorId"]), ["V001", "V002"])
        self.assertEqual(vendors.loc[0, "vendorName"], "Fujitsu")
        self.assertEqual(vendors.loc[0, "Description"], "Fujitsu")
        self.assertEqual(vendors.loc[1, "isActive"], "N")

    def test_template_inventory_transfers_pairs_stock_audit_im_rows(self) -> None:
        stock_audit = pd.DataFrame(
            {
                "Item No.": ["A1", ""],
                "Description": ["Part A", ""],
                "Posting Date": ["31/07/23", "31/07/23"],
                "Document": ["IM 50000431", "IM 50000431"],
                "Whse": ["FUJH&M-J", "FUJH&M-C"],
                "Quantity": ["-1.000", "1.000"],
            }
        )
        transfers = build_template_inventory_transfers(
            stock_audit,
            [
                "inventoryTransferImoId",
                "createdDateTime",
                "completedDateTime",
                "fromWarehouseId",
                "toWarehouseId",
                "demandStatus",
                "orderStatusIsClosed",
                "movementType",
                "addressId",
                "isResolved",
                "partNumber",
                "quantity",
                "shipListCode",
            ],
        )
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers.loc[0, "inventoryTransferImoId"], "IM 50000431|A1|1")
        self.assertEqual(transfers.loc[0, "createdDateTime"], "2023-07-31")
        self.assertEqual(transfers.loc[0, "completedDateTime"], "2023-07-31")
        self.assertEqual(transfers.loc[0, "fromWarehouseId"], "FUJH&M-J")
        self.assertEqual(transfers.loc[0, "toWarehouseId"], "FUJH&M-C")
        self.assertEqual(transfers.loc[0, "demandStatus"], "Fulfilled")
        self.assertEqual(transfers.loc[0, "orderStatusIsClosed"], "Y")
        self.assertEqual(transfers.loc[0, "movementType"], "Internal_move")
        self.assertEqual(transfers.loc[0, "addressId"], "")
        self.assertEqual(transfers.loc[0, "isResolved"], "Y")
        self.assertEqual(transfers.loc[0, "partNumber"], "A1")
        self.assertEqual(transfers.loc[0, "quantity"], 1)

    def test_template_inventory_transfers_pairs_duplicate_same_quantity_by_row_order(self) -> None:
        stock_audit = pd.DataFrame(
            {
                "Item No.": ["A1", "", "", ""],
                "Posting Date": ["20/10/23", "20/10/23", "20/10/23", "20/10/23"],
                "Document": ["IM 50000464", "IM 50000464", "IM 50000464", "IM 50000464"],
                "Whse": ["FUJITSU", "FUJH&M-C", "FUJITSU", "FUJH&M-J"],
                "Quantity": ["-1.000", "1.000", "-1.000", "1.000"],
            }
        )
        transfers = build_template_inventory_transfers(
            stock_audit,
            ["inventoryTransferImoId", "fromWarehouseId", "toWarehouseId", "partNumber", "quantity"],
        )
        self.assertEqual(list(transfers["inventoryTransferImoId"]), ["IM 50000464|A1|1|1", "IM 50000464|A1|1|2"])
        self.assertEqual(list(transfers["toWarehouseId"]), ["FUJH&M-C", "FUJH&M-J"])

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

    def test_excluded_choice_and_mxt_warehouses_are_removed_from_sources(self) -> None:
        warehouses = pd.DataFrame(
            {
                "WarehouseCode": ["FUJITSU", "CHLJHB", "CHOICE", "GCJRMA", "MXT"],
                "WarehouseName": ["Main", "Choice Logistics Main", "Choice Logistics Warehouse", "Choice RMA Warehouse", "Maxtec Waehouse"],
            }
        )
        inventory = pd.DataFrame({"WarehouseCode": ["FUJITSU", "CHLCT", "MXT"], "ItemNo": ["A", "B", "C"]})
        usage = pd.DataFrame({"WarehouseCode": ["FUJITSU", "CHOICE"], "ItemNo": ["A", "B"]})
        purchase_orders = pd.DataFrame({"ToWarehouseId": ["FUJITSU", "CHLJHB", "MXT"], "PartNumber": ["A", "B", "C"]})
        stock_flow = pd.DataFrame({"Whse": ["FUJITSU", "GCJRMA"], "Item No.": ["A", "B"]})
        manual = pd.DataFrame({"warehouseId": ["FUJITSU", "CHLJHB"], "warehouseDescription": ["Main", "Choice Logistics Main"]})

        filtered = filter_excluded_warehouse_sources(warehouses, inventory, usage, purchase_orders, stock_flow, manual)

        self.assertEqual(list(filtered[0]["WarehouseCode"]), ["FUJITSU"])
        self.assertEqual(list(filtered[1]["WarehouseCode"]), ["FUJITSU"])
        self.assertEqual(list(filtered[2]["WarehouseCode"]), ["FUJITSU"])
        self.assertEqual(list(filtered[3]["ToWarehouseId"]), ["FUJITSU"])
        self.assertEqual(list(filtered[4]["Whse"]), ["FUJITSU"])
        self.assertEqual(list(filtered[5]["warehouseId"]), ["FUJITSU"])

    def test_template_warehouses_maps_manual_answers_and_inverts_obsolete_status(self) -> None:
        warehouses = pd.DataFrame({"WarehouseCode": ["FUJITSU", "FUJ CT"], "WarehouseName": ["Main", "Old"]})
        manual = pd.DataFrame(
            {
                "warehouseId": ["FUJITSU", "FUJ CT"],
                "addressId": ["OLD1", "OLD2"],
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

        self.assertEqual(out.loc[0, "addressId"], "FUJITSU")
        self.assertEqual(out.loc[1, "addressId"], "FUJ CT")
        self.assertEqual(out.loc[0, "warehouseStatusId"], "Y")
        self.assertEqual(out.loc[1, "warehouseStatusId"], "N")

    def test_template_warehouses_maps_shared_physical_addresses(self) -> None:
        warehouses = pd.DataFrame(
            {
                "WarehouseCode": ["FUJITSU", "FSCGREEN", "BNI CT", "FUJSANCT", "FUJMSM J"],
                "WarehouseName": ["Main JHB", "Green JHB", "BNI CT", "Sanlam CT", "Massmart JHB"],
            }
        )

        out = build_template_warehouses(warehouses, ["warehouseId", "addressId", "warehouseDescription"])
        by_id = out.set_index("warehouseId")

        self.assertEqual(by_id.loc["FUJITSU", "addressId"], "FUJITSU")
        self.assertEqual(by_id.loc["FSCGREEN", "addressId"], "FUJITSU")
        self.assertEqual(by_id.loc["FUJMSM J", "addressId"], "FUJITSU")
        self.assertEqual(by_id.loc["BNI CT", "addressId"], "FUJ CT")
        self.assertEqual(by_id.loc["FUJSANCT", "addressId"], "FUJ CT")

    def test_template_parts_defaults_missing_main_alternative_to_primary(self) -> None:
        parts = pd.DataFrame({"ItemNo": ["A1"], "SPLMaster": ["SPL1"], "ItemDescription": ["Part A"], "DisplayItemNo": ["A1"]})
        out = build_template_parts(parts, ["SPLMaster", "PartNumber", "isPrimary", "primaryPartNumber", "description"])
        self.assertEqual(out.loc[0, "SPLMaster"], "")
        self.assertEqual(out.loc[0, "Primary Part"], "A1")
        self.assertEqual(out.loc[0, "B_Part"], "A1")
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
        self.assertEqual(out.loc[1, "Primary Part"], "34076947")
        self.assertEqual(out.loc[1, "B_Part"], "38049457")
        self.assertEqual(out.loc[1, "primaryPartNumber"], "34076947")

    def test_template_parts_removes_exact_duplicate_rows(self) -> None:
        parts = pd.DataFrame(
            {
                "ItemNo": ["A1", "A1"],
                "ItemDescription": ["Part A", "Part A"],
                "DisplayDescription": ["", ""],
            }
        )

        out = build_template_parts(parts, ["PartNumber", "description"])

        self.assertEqual(len(out), 1)
        self.assertEqual(out.loc[0, "B_Part"], "A1")

    def test_cost_category_uses_approved_co_cre8_cost_bands(self) -> None:
        self.assertEqual(_cost_category("0.01"), "A")
        self.assertEqual(_cost_category("58.70"), "A")
        self.assertEqual(_cost_category("58.71"), "B")
        self.assertEqual(_cost_category("165.65"), "B")
        self.assertEqual(_cost_category("282.98"), "C")
        self.assertEqual(_cost_category("441.62"), "D")
        self.assertEqual(_cost_category("900.01"), "E")
        self.assertEqual(_cost_category("900.02"), "F")
        self.assertEqual(_cost_category("0"), "")

    def test_template_parts_populates_cost_category_from_spi_cost(self) -> None:
        parts = pd.DataFrame({"ItemNo": ["A1", "B2", "C3"], "ItemDescription": ["A", "B", "C"]})
        spi = pd.DataFrame(
            {
                "PartNumber": ["A1", "B2"],
                "Material": ["", ""],
                "ListPrice": ["81.53", "1250.02"],
                "SourceDate": ["2026-05-01", "2026-05-01"],
            }
        )

        out = build_template_parts(parts, ["PartNumber", "costCategory"], spi)

        self.assertEqual(list(out["costCategory"]), ["A", "E", ""])

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
                "isKit",
                "isTool",
                "isExcludeFromReplenishment",
                "purchaseLeadTimeDays",
                "isCritical",
            ],
        )

        self.assertEqual(out.loc[0, "isBootStockable"], "N")
        self.assertEqual(out.loc[0, "isBranchStockable"], "Y")
        self.assertEqual(out.loc[0, "isObsolete"], "N")
        self.assertEqual(out.loc[0, "isKit"], "N")
        self.assertEqual(out.loc[0, "isTool"], "N")
        self.assertEqual(out.loc[0, "isExcludeFromReplenishment"], "N")
        self.assertEqual(out.loc[0, "purchaseLeadTimeDays"], 180)
        self.assertEqual(out.loc[0, "isCritical"], "Yes")
        self.assertEqual(out.loc[1, "description"], "No Description")
        self.assertEqual(out.loc[1, "isKit"], "N")
        self.assertEqual(out.loc[1, "isTool"], "N")
        self.assertEqual(out.loc[1, "purchaseLeadTimeDays"], 3)
        self.assertEqual(out.loc[1, "isCritical"], "Yes")

    def test_template_parts_marks_kits_and_confirmed_tools(self) -> None:
        parts = pd.DataFrame(
            {
                "ItemNo": ["1531813", "1534363", "A3", "A4"],
                "ItemDescription": ["Diagnostic set", "Tool item", "Printer KIT", "Spare"],
                "DisplayDescription": ["", "", "", ""],
            }
        )
        altsgen = pd.DataFrame(
            {
                "PartNumber": ["A4"],
                "status": ["ok"],
                "partType": ["maintenance_kit"],
                "canonicalDescription": ["Maintenance Kit"],
            }
        )

        out = build_template_parts(parts, ["PartNumber", "description", "productType", "partType", "isKit", "isTool"], altsgen_evidence=altsgen)

        self.assertEqual(list(out["isTool"]), ["Y", "Y", "N", "N"])
        self.assertEqual(list(out["isKit"]), ["N", "N", "Y", "Y"])

    def test_template_parts_populates_altsgen_fields_from_evidence(self) -> None:
        parts = pd.DataFrame({"ItemNo": ["A1", "B2", "C3"], "ItemDescription": ["Drive", "Board", "Battery"]})
        altsgen = pd.DataFrame(
            {
                "PartNumber": ["A1", "B2", "C3"],
                "status": ["ok", "ok", "error"],
                "partType": ["hdd_sas_2_5_sff", "laptop_motherboard", "laptop_battery"],
                "canonicalDescription": ["2.5 inch HDD", "Fujitsu Laptop Motherboard", "Laptop Battery"],
            }
        )

        out = build_template_parts(
            parts,
            ["PartNumber", "productClass", "productType", "partType"],
            altsgen_evidence=altsgen,
        )

        self.assertEqual(out.loc[0, "partType"], "hdd_sas_2_5_sff")
        self.assertEqual(out.loc[0, "productType"], "2.5 inch HDD")
        self.assertEqual(out.loc[0, "productClass"], "STORAGE")
        self.assertEqual(out.loc[1, "productType"], "Fujitsu Laptop Motherboard")
        self.assertEqual(out.loc[1, "productClass"], "BOARD")
        self.assertEqual(out.loc[2, "partType"], "unknown_component")
        self.assertEqual(out.loc[2, "productType"], "Unknown Component")
        self.assertEqual(out.loc[2, "productClass"], "OTHER")

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
        self.assertEqual(list(out["Pu Part"]), ["A1", "A1"])
        self.assertEqual(list(out["Part Code"]), ["A1", "A1"])
        self.assertEqual(list(out["Pu Warehouse Code"]), ["WH1", "WH1"])
        self.assertEqual(list(out["Pu Warehouse"]), ["WH1", "WH1"])
        self.assertEqual(list(out["quantityUsed"]), [2, 1])
        self.assertEqual(out.loc[0, "partsUsedDateTime"], "2026-02-01")

    def test_template_warehouses_adds_primary_flag(self) -> None:
        warehouses = pd.DataFrame(
            {
                "WarehouseCode": ["FUJITSU", "FUJ CT", "ACER"],
                "WarehouseName": ["Main JHB", "Main CT", "Acer"],
            }
        )

        out = build_template_warehouses(warehouses, ["warehouseId", "warehouseDescription"])

        self.assertEqual(list(out["isPrimary"]), ["Y", "N", "N"])

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
                "OrderType",
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
        self.assertEqual(out.loc[0, "Pu Part"], "38066709")
        self.assertEqual(out.loc[0, "Part Code"], "38066709")
        self.assertEqual(out.loc[0, "Pu Warehouse Code"], "FUJ CT")
        self.assertEqual(out.loc[0, "Pu Warehouse"], "FUJ CT")
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

    def test_template_outputs_uses_stock_audit_parts_usage_base_over_dn_context(self) -> None:
        dn_context = pd.DataFrame(
            {
                "DeliveryNoteNumber": ["50006687"],
                "DocDate": ["20260206"],
                "CustomerRefNumber": ["72749474"],
                "Comments": ["Call Nr 72749474"],
                "ItemNo": ["38066705"],
                "WarehouseCode": ["FUJ CT"],
                "Quantity": [8],
            }
        )
        stock_audit = pd.DataFrame(
            {
                "Item No.": ["38066705", ""],
                "Description": ["Part", ""],
                "Posting Date": ["06/02/26", "13/02/26"],
                "Document": ["DN 50006687", "DN 50006712"],
                "Whse": ["FUJ CT", "FUJ CT"],
                "Quantity": ["-8", "-1"],
            }
        )

        outputs = build_template_outputs(
            {"PartsUsage": ["orderNumber", "requestId", "partCode", "quantityUsed", "partsUsedDateTime"]},
            pd.DataFrame(),
            dn_context,
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            masters=pd.DataFrame(),
            parts_usage_source=stock_audit,
            delivery_note_context=dn_context,
        )

        out = outputs["PartsUsage"]
        self.assertEqual(len(out), 2)
        self.assertEqual(list(out["partCode"]), ["38066705", "38066705"])
        self.assertEqual(list(out["quantityUsed"]), [8, 1])
        self.assertEqual(list(out["partsUsedDateTime"]), ["2026-02-06", "2026-02-13"])
        self.assertEqual(out.loc[0, "orderNumber"], "DN 50006687")
        self.assertEqual(out.loc[0, "requestId"], "72749474")
        self.assertEqual(out.loc[1, "orderNumber"], "DN 50006712")

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
                "OrderType",
                "customerCompanyCode",
                "orderStartDatetime",
                "orderStatus",
                "partCode",
                "serialNumber",
                "quantityUsed",
                "partsUsedDateTime",
                "Warehouse",
                "Warehouse Code",
                "deviceSerialNumber",
                "Master",
            ],
            masters,
            pd.DataFrame({"Call Number": ["67548724"], "Created": ["2025/10/15"], "Status": ["Closed"]}),
            pd.DataFrame({"WarehouseCode": ["FUJITSU"], "WarehouseName": ["CC8 - MAIN - JHB - SPL"]}),
        )

        self.assertEqual(out.loc[0, "orderNumber"], "DN 50006351")
        self.assertEqual(out.loc[0, "requestId"], "67548724")
        self.assertEqual(out.loc[0, "OrderType"], "service_order")
        self.assertEqual(out.loc[0, "customerCompanyCode"], "Massmart")
        self.assertEqual(out.loc[0, "orderStartDatetime"], "2025-10-15")
        self.assertEqual(out.loc[0, "orderStatus"], "Closed")
        self.assertEqual(out.loc[0, "partCode"], "38062982")
        self.assertEqual(out.loc[0, "Pu Part"], "38062982")
        self.assertEqual(out.loc[0, "Part Code"], "38062982")
        self.assertEqual(out.loc[0, "serialNumber"], "YM6D008491")
        self.assertEqual(out.loc[0, "deviceSerialNumber"], "YM6D008491")
        self.assertEqual(out.loc[0, "quantityUsed"], 1)
        self.assertEqual(out.loc[0, "partsUsedDateTime"], "2025-10-17")
        self.assertEqual(out.loc[0, "Warehouse"], "CC8 - MAIN - JHB - SPL")
        self.assertEqual(out.loc[0, "Warehouse Code"], "FUJITSU")
        self.assertEqual(out.loc[0, "Pu Warehouse Code"], "FUJITSU")
        self.assertEqual(out.loc[0, "Pu Warehouse"], "FUJITSU")
        self.assertEqual(out.loc[0, "Master"], "SPL1")

    def test_template_parts_usage_matches_helpdesk_status_with_customer_prefix(self) -> None:
        usage = pd.DataFrame(
            {
                "DeliveryNoteNumber": ["1"],
                "DocDate": ["20260527"],
                "CustomerRefNumber": ["Acer:74782263 - 539904"],
                "Comments": ["Customer: Acer"],
                "ItemNo": ["DB.VYQ11.002"],
                "WarehouseCode": ["ACERKH"],
                "Quantity": [1],
            }
        )

        out = build_template_parts_usage(
            usage,
            ["orderNumber", "orderStartDatetime", "orderStatus", "Warehouse", "Warehouse Code"],
            None,
            pd.DataFrame(
                {
                    "Call Number": ["74782263 - 539904"],
                    "Created": ["2026-05-27T05:52:54Z"],
                    "Status": ["DeliveryNote"],
                }
            ),
            pd.DataFrame({"WarehouseCode": ["ACERKH"], "WarehouseName": ["CC8 - ACER - KHAULEZA - JHB - SPL - OFFSITE"]}),
        )

        self.assertEqual(out.loc[0, "orderStatus"], "DeliveryNote")
        self.assertEqual(out.loc[0, "orderStartDatetime"], "2026-05-27")
        self.assertEqual(out.loc[0, "Warehouse"], "CC8 - ACER - KHAULEZA - JHB - SPL - OFFSITE")
        self.assertEqual(out.loc[0, "Warehouse Code"], "ACERKH")

    def test_template_service_orders_uses_helpdesk_tickets_and_spares_actual_eta(self) -> None:
        spares = pd.DataFrame(
            {
                "Cust Ord No": ["S_71919593", "S_71919593", "S_71942838", "S_88888888"],
                "Order Date": ["20/01/2026", "20/01/2026", "28/01/2026", "02/02/2026"],
                "Order Time": ["1631", "1700", "1403", "0815"],
                "Del Date": ["20/01/2026", "21/01/2026", "28/01/2026", "03/02/2026"],
                "Del Time": ["1631", "0900", "1404", "0915"],
                "Del No.": ["50001001", "50001002", "50001003", "50001004"],
                "Customer ": ["WCED", "WCED", "SENWES", "SPARES ONLY CUSTOMER"],
                "Customer Name": ["DW PC CORPORATION", "DW PC CORPORATION", "COCRE8 TECHNOLOGY SOLUTIONS", "SPARES ONLY CUSTOMER"],
            }
        )
        issue_tracker = pd.DataFrame(
            {
                "Call Number": ["71919593", "71942838", "77782146", ""],
                "Subject": ["", "", "", "ACER Spares || Ticket 72759481 || 00534758"],
                "Created": ["2026-01-20T16:31:00Z", "2026-01-28T14:03:00Z", "2026-05-25T19:33:38Z", "2026-05-07T08:32:29Z"],
                "Status": ["Closed", "Closed", "Open", "Closed"],
                "SLA": ["8 Hours Recovery 24x7", "NBD response, 9x5", "8 Hours Recovery 24x7", "NBD response, 9x5"],
                "DeliveryCity": ["Cape Town", "JHB", "Unknown", "JHB"],
                "Customer": ["WCED", "SENWES", "Sanlam", "Department of Justice"],
                "CustomerNormalized": ["WCED", "Other", "SANLAM", "Other"],
            }
        )

        out = build_template_service_orders(
            spares,
            [
                "orderNumber",
                "RequestID",
                "location",
                "eta",
                "actualEta",
                "actualResolveDateTime",
                "recallDateTime",
                "actualRecallDateTime",
                "slaEtaClock",
                "slaResolveClock",
                "slaFailureCode",
                "slaEtaHit",
                "slaResolveDateTime",
            ],
            issue_tracker,
        )

        self.assertEqual(len(out), 5)
        self.assertEqual(out.loc[0, "orderNumber"], "DN 50001001")
        self.assertEqual(out.loc[0, "RequestID"], "71919593")
        self.assertEqual(out.loc[0, "location"], "Cape Town")
        self.assertEqual(out.loc[0, "actualEta"], "2026-01-20 16:31:00")
        self.assertEqual(out.loc[0, "slaEtaClock"], "8")
        self.assertEqual(out.loc[0, "slaResolveClock"], "8")
        self.assertEqual(out.loc[0, "slaResolveDateTime"], "2026-01-21 00:31:00")
        self.assertEqual(out.loc[1, "orderNumber"], "DN 50001003")
        self.assertEqual(out.loc[1, "RequestID"], "71942838")
        self.assertEqual(out.loc[1, "location"], "JHB")
        self.assertEqual(out.loc[1, "slaResolveClock"], "")
        self.assertEqual(out.loc[2, "orderNumber"], "77782146")
        self.assertEqual(out.loc[2, "RequestID"], "77782146")
        self.assertEqual(out.loc[2, "location"], "SANLAM")
        self.assertEqual(out.loc[2, "actualEta"], "")
        self.assertEqual(out.loc[3, "orderNumber"], "72759481")
        self.assertEqual(out.loc[3, "RequestID"], "72759481")
        self.assertEqual(out.loc[3, "location"], "JHB")
        self.assertEqual(out.loc[4, "orderNumber"], "DN 50001004")
        self.assertEqual(out.loc[4, "RequestID"], "88888888")
        self.assertEqual(out.loc[4, "location"], "SPARES ONLY CUSTOMER")
        self.assertEqual(out.loc[4, "actualEta"], "2026-02-03 09:15:00")

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

    def test_template_part_types_uses_altsgen_commodity_lookup(self) -> None:
        evidence = pd.DataFrame(
            {
                "PartNumber": ["A1", "B2", "C3", "D4"],
                "status": ["ok", "ok", "error", "ok"],
                "partType": ["hdd_sas_2_5_sff", "hdd_sas_2_5_sff", "laptop_battery", "unknown"],
                "partTypeDescription": ["SAS 2.5 inch hard disk drive", "Duplicate", "Battery", "Unknown"],
                "isReworkable": ["", "", "", ""],
            }
        )

        out = build_template_part_types(evidence, ["partType", "partTypeDescription", "isReworkable"])

        self.assertEqual(len(out), 2)
        self.assertEqual(out.loc[0, "partType"], "hdd_sas_2_5_sff")
        self.assertEqual(out.loc[0, "partTypeDescription"], "Duplicate")
        self.assertEqual(out.loc[0, "isReworkable"], "NO")
        self.assertEqual(out.loc[1, "partType"], "unknown_component")
        self.assertEqual(out.loc[1, "partTypeDescription"], "Unknown Component")
        self.assertEqual(out.loc[1, "isReworkable"], "NO")

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

    def test_template_validation_applies_confirmed_object_decisions(self) -> None:
        templates = {
            "Models": ["modelId"],
            "RepairOrder": ["repairOrderId"],
            "WarehouseExclusions": ["warehouseId"],
        }
        outputs = {name: pd.DataFrame(columns=columns) for name, columns in templates.items()}

        result = validate_template_outputs(Path("out"), outputs, templates)
        by_object = result.set_index("Object")

        self.assertEqual(by_object.loc["Models", "Status"], "DEFERRED")
        self.assertEqual(by_object.loc["RepairOrder", "Status"], "OUT_OF_SCOPE")
        self.assertEqual(by_object.loc["WarehouseExclusions", "Status"], "PASS")

    def test_template_validation_still_fails_bad_schema_for_deferred_objects(self) -> None:
        templates = {"Models": ["modelId"]}
        outputs = {"Models": pd.DataFrame(columns=["wrong"])}

        result = validate_template_outputs(Path("out"), outputs, templates)

        self.assertEqual(result.loc[0, "Status"], "FAIL")

    def test_warehouse_exclusions_block_cross_pool_stock_sharing(self) -> None:
        warehouses = pd.DataFrame(
            {
                "WarehouseCode": ["FUJITSU", "FUJ CT", "FUJMSM C", "FUJMSM J", "ACER", "CHL001", "MXT"],
            }
        )

        out = build_template_warehouse_exclusions(
            warehouses,
            ["Secondary TO", "Secondary FROM", "Cross TO", "Cross FROM", "Primary TO"],
        )

        secondary_pairs = set(zip(out["Secondary TO"], out["Secondary FROM"]))
        cross_pairs = set(zip(out["Cross TO"], out["Cross FROM"]))
        self.assertNotIn(("FUJITSU", "FUJ CT"), secondary_pairs)
        self.assertNotIn(("FUJMSM C", "FUJMSM J"), secondary_pairs)
        self.assertIn(("FUJITSU", "FUJMSM C"), secondary_pairs)
        self.assertIn(("FUJMSM C", "FUJITSU"), secondary_pairs)
        self.assertIn(("ACER", "FUJITSU"), secondary_pairs)
        self.assertEqual(secondary_pairs, cross_pairs)
        self.assertEqual(set(out["Primary TO"]), {""})
        self.assertFalse(out.isin(["CHL001", "MXT"]).any().any())


if __name__ == "__main__":
    unittest.main()
