"""Strict, evidence-first helpers for the CoCre8 HelpDesk issue export."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").strip()
    if text.lower() in {"none", "nan", "null", "<na>"}:
        return ""
    return text


def part_key(value: object) -> str:
    text = clean_text(value)
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text.upper()


def po_key(value: object) -> str:
    """Normalize an explicit PO/order-confirmation value without fuzzy matching."""

    text = clean_text(value).upper()
    if not text:
        return ""
    text = re.sub(r"\.(PDF|XLSX?|DOCX?)$", "", text)
    tokens = [token for token in re.findall(r"[A-Z0-9]+", text) if token not in {"PDF", "XLS", "XLSX", "DOC", "DOCX"}]
    if len(tokens) == 1:
        return tokens[0].lstrip("0") or "0"
    numeric_tokens = [token for token in tokens if token.isdigit()]
    if len(numeric_tokens) == 1:
        return numeric_tokens[0].lstrip("0") or "0"
    return ""


def read_issue_tracker(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    for encoding in ["utf-8-sig", "cp1252", "latin-1"]:
        try:
            df = pd.read_csv(path, dtype=str, encoding=encoding).fillna("")
            return normalize_issue_tracker(df)
        except UnicodeDecodeError:
            continue
    return pd.DataFrame()


def normalize_issue_tracker(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy().fillna("")
    for column in work.columns:
        work[column] = work[column].map(clean_text)
    for column in [
        "Call Number",
        "Created",
        "Status",
        "Customer",
        "Part Nr",
        "Quantity",
        "WorkOrder",
        "PurchaseOrder",
        "ReplenishStatus",
        "SPLMaster",
        "Serial Nr",
        "DeliveryCity",
        "DispatchWarehouse",
        "DispatchPartNo",
        "CustomerNormalized",
        "DeliveryAddress",
        "MSConvoID",
    ]:
        if column not in work.columns:
            work[column] = ""
    work["PartKey"] = work["Part Nr"].map(part_key)
    work["DispatchPartKey"] = work["DispatchPartNo"].map(part_key)
    work["PurchaseOrderKey"] = work["PurchaseOrder"].map(po_key)
    work["ConversationKey"] = work["MSConvoID"].astype(str).str.strip()
    work["TicketPartEvidenceKey"] = work["Call Number"].astype(str).str.strip() + "|" + work["PartKey"]
    return work


def master_lookup(masters: pd.DataFrame | None) -> dict[str, str]:
    if masters is None or masters.empty or "SPL Master" not in masters.columns or "Items linked" not in masters.columns:
        return {}
    lookup: dict[str, str] = {}
    for _, row in masters.iterrows():
        master = clean_text(row.get("SPL Master"))
        if not master:
            continue
        for item in clean_text(row.get("Items linked")).split(";"):
            key = part_key(item)
            if key:
                lookup.setdefault(key, master)
    return lookup


def issue_tracker_evidence_rows(issue_tracker: pd.DataFrame, masters: pd.DataFrame | None) -> pd.DataFrame:
    if issue_tracker.empty:
        return pd.DataFrame(
            columns=[
                "CallNumber",
                "MSConvoID",
                "Created",
                "Status",
                "Customer",
                "CustomerNormalized",
                "RequestedPartNumber",
                "DispatchPartNumber",
                "SPLMaster",
                "MappedSPLMaster",
                "Quantity",
                "PurchaseOrder",
                "ReplenishStatus",
                "WorkOrder",
                "SerialNumber",
                "DeliveryCity",
                "DeliveryAddress",
                "EvidenceStatus",
            ]
        )
    by_part = master_lookup(masters)
    requested_master = issue_tracker["PartKey"].map(by_part).fillna("")
    dispatch_master = issue_tracker["DispatchPartKey"].map(by_part).fillna("")
    mapped_master = requested_master.where(requested_master.astype(str).str.strip().ne(""), dispatch_master)
    out = pd.DataFrame(
        {
            "CallNumber": issue_tracker["Call Number"],
            "MSConvoID": issue_tracker["MSConvoID"],
            "Created": issue_tracker["Created"],
            "Status": issue_tracker["Status"],
            "Customer": issue_tracker["Customer"],
            "CustomerNormalized": issue_tracker["CustomerNormalized"],
            "RequestedPartNumber": issue_tracker["Part Nr"].map(part_key),
            "DispatchPartNumber": issue_tracker["DispatchPartNo"].map(part_key),
            "SPLMaster": issue_tracker["SPLMaster"],
            "MappedSPLMaster": mapped_master,
            "Quantity": issue_tracker["Quantity"],
            "PurchaseOrder": issue_tracker["PurchaseOrder"],
            "ReplenishStatus": issue_tracker["ReplenishStatus"],
            "WorkOrder": issue_tracker["WorkOrder"],
            "SerialNumber": issue_tracker["Serial Nr"],
            "DeliveryCity": issue_tracker["DeliveryCity"],
            "DeliveryAddress": issue_tracker["DeliveryAddress"],
        }
    )
    out["EvidenceStatus"] = "Tracker row only"
    out.loc[out["MappedSPLMaster"].astype(str).str.strip().eq(""), "EvidenceStatus"] = "Needs SPL Master mapping"
    return out


def purchase_order_ticket_matches(
    purchase_orders: pd.DataFrame,
    issue_tracker: pd.DataFrame,
    masters: pd.DataFrame | None,
) -> pd.DataFrame:
    columns = [
        "PurchaseOrderNumber",
        "SapPartNumber",
        "SapSPLMaster",
        "TicketPartNumber",
        "TicketDispatchPartNumber",
        "TicketSPLMaster",
        "CallNumber",
        "MSConvoID",
        "ReplenishStatus",
        "MatchType",
        "Evidence",
    ]
    if purchase_orders.empty or issue_tracker.empty:
        return pd.DataFrame(columns=columns)

    by_part = master_lookup(masters)
    tracker = issue_tracker.copy()
    tracker = tracker[tracker["PurchaseOrderKey"].astype(str).str.strip().ne("")]
    if tracker.empty:
        return pd.DataFrame(columns=columns)
    tracker["TicketMaster"] = tracker["PartKey"].map(by_part).fillna("")
    tracker["DispatchMaster"] = tracker["DispatchPartKey"].map(by_part).fillna("")

    rows: list[dict[str, str]] = []
    for _, po in purchase_orders.iterrows():
        po_number = po_key(po.get("PurchaseOrderNumber"))
        sap_part = part_key(po.get("PartNumber"))
        sap_master = by_part.get(sap_part, "")
        if not po_number:
            continue
        candidates = tracker[tracker["PurchaseOrderKey"].eq(po_number)]
        for _, ticket in candidates.iterrows():
            ticket_part = part_key(ticket.get("Part Nr"))
            dispatch_part = part_key(ticket.get("DispatchPartNo"))
            ticket_master = clean_text(ticket.get("TicketMaster")) or clean_text(ticket.get("DispatchMaster"))
            match_type = ""
            if sap_part and sap_part in {ticket_part, dispatch_part}:
                match_type = "Exact PO + exact part"
            elif sap_master and ticket_master and sap_master == ticket_master:
                match_type = "Exact PO + same SPL Master"
            if not match_type:
                continue
            rows.append(
                {
                    "PurchaseOrderNumber": po_number,
                    "SapPartNumber": sap_part,
                    "SapSPLMaster": sap_master,
                    "TicketPartNumber": ticket_part,
                    "TicketDispatchPartNumber": dispatch_part,
                    "TicketSPLMaster": ticket_master,
                    "CallNumber": clean_text(ticket.get("Call Number")),
                    "MSConvoID": clean_text(ticket.get("MSConvoID")),
                    "ReplenishStatus": clean_text(ticket.get("ReplenishStatus")),
                    "MatchType": match_type,
                    "Evidence": "Issue tracker row matched by explicit PurchaseOrder plus part/master evidence.",
                }
            )
    return pd.DataFrame(rows, columns=columns).drop_duplicates()


def purchase_order_reconciliation(
    purchase_orders: pd.DataFrame,
    issue_tracker: pd.DataFrame,
    masters: pd.DataFrame | None,
) -> pd.DataFrame:
    columns = [
        "TicketPurchaseOrder",
        "SapPurchaseOrder",
        "TicketPartNumber",
        "TicketDispatchPartNumber",
        "SapPartNumber",
        "TicketSPLMaster",
        "SapSPLMaster",
        "CallNumber",
        "MSConvoID",
        "ReplenishStatus",
        "MatchStatus",
        "Evidence",
    ]
    if issue_tracker.empty:
        return pd.DataFrame(columns=columns)

    by_part = master_lookup(masters)
    sap = purchase_orders.copy() if not purchase_orders.empty else pd.DataFrame()
    if sap.empty:
        sap = pd.DataFrame(columns=["PurchaseOrderNumber", "PartNumber"])
    sap["SapPurchaseOrderKey"] = sap.get("PurchaseOrderNumber", pd.Series([""] * len(sap))).map(po_key)
    sap["SapPartKey"] = sap.get("PartNumber", pd.Series([""] * len(sap))).map(part_key)
    sap["SapSPLMaster"] = sap["SapPartKey"].map(by_part).fillna("")

    tracker = issue_tracker[issue_tracker["PurchaseOrderKey"].astype(str).str.strip().ne("")].copy()
    rows: list[dict[str, str]] = []
    for _, ticket in tracker.iterrows():
        ticket_po = clean_text(ticket.get("PurchaseOrderKey"))
        ticket_part = part_key(ticket.get("Part Nr"))
        ticket_dispatch = part_key(ticket.get("DispatchPartNo"))
        ticket_master = clean_text(ticket.get("SPLMaster")) or by_part.get(ticket_part, "") or by_part.get(ticket_dispatch, "")
        candidates = sap[sap["SapPurchaseOrderKey"].eq(ticket_po)]
        if candidates.empty:
            rows.append(
                {
                    "TicketPurchaseOrder": ticket_po,
                    "SapPurchaseOrder": "",
                    "TicketPartNumber": ticket_part,
                    "TicketDispatchPartNumber": ticket_dispatch,
                    "SapPartNumber": "",
                    "TicketSPLMaster": ticket_master,
                    "SapSPLMaster": "",
                    "CallNumber": clean_text(ticket.get("Call Number")),
                    "MSConvoID": clean_text(ticket.get("MSConvoID")),
                    "ReplenishStatus": clean_text(ticket.get("ReplenishStatus")),
                    "MatchStatus": "No matching SAP PO number",
                    "Evidence": "HelpDesk PurchaseOrder filename stem does not match any SAP OPOR.NumAtCard returned by the live extract.",
                }
            )
            continue
        matched_any_part = False
        for _, sap_row in candidates.iterrows():
            sap_part = clean_text(sap_row.get("SapPartKey"))
            sap_master = clean_text(sap_row.get("SapSPLMaster"))
            if sap_part and sap_part in {ticket_part, ticket_dispatch}:
                status = "Matched SAP PO and exact part"
                matched_any_part = True
            elif sap_master and ticket_master and sap_master == ticket_master:
                status = "Matched SAP PO and SPL Master"
                matched_any_part = True
            else:
                status = "SAP PO matched; part/master mismatch"
            rows.append(
                {
                    "TicketPurchaseOrder": ticket_po,
                    "SapPurchaseOrder": clean_text(sap_row.get("PurchaseOrderNumber")),
                    "TicketPartNumber": ticket_part,
                    "TicketDispatchPartNumber": ticket_dispatch,
                    "SapPartNumber": sap_part,
                    "TicketSPLMaster": ticket_master,
                    "SapSPLMaster": sap_master,
                    "CallNumber": clean_text(ticket.get("Call Number")),
                    "MSConvoID": clean_text(ticket.get("MSConvoID")),
                    "ReplenishStatus": clean_text(ticket.get("ReplenishStatus")),
                    "MatchStatus": status,
                    "Evidence": "SAP OPOR.NumAtCard equals HelpDesk PurchaseOrder filename stem; part/master checked separately.",
                }
            )
        if not matched_any_part and not candidates.empty:
            continue
    return pd.DataFrame(rows, columns=columns).drop_duplicates()
