# CoCre8 HelpDesk Flow Recommendations

This review is based on `Reference/HelpdeskFlow.txt`, the CoCre8 issue tracker export, and the sample email threads in `Reference/helpdesk_emails`.

## Immediate Fixes

- Rotate the OpenAI API key currently embedded in the Power Automate export, then move the key to a secure connection, environment variable, or Key Vault backed secret.
- Store `SPLMaster` on every ticket line at creation time, using the same `masters.csv`/SPI mapping used by onboarding. Keep the requested part number separately.
- Store separate part fields for `RequestedPartNumber`, `DispatchedPartNumber`, `ReturnedFaultyPartNumber`, and `ReplenishmentPartNumber`. Do not overwrite one with another.
- Capture `DeliveryNoteNumber` as a structured field when SPL sends or attaches a DN, because it is the best bridge to SAP Stock Audit usage rows.
- Capture Fujitsu order confirmation number as its own field, separate from CoCre8 PO number. The current flow already looks for `800...` values, but the lifecycle needs it as a first-class field.

## Ticket Lifecycle

Use separate lifecycle/status fields rather than one overloaded status:

- `OrderRequestStatus`: new, acknowledged, missing info, cancelled.
- `DispatchStatus`: pending pick, picked, delivered, collected, cancelled.
- `FaultyReturnStatus`: not expected, pending return, collection arranged, returned, overdue, unknown.
- `ReplenishmentStatus`: not required, requested, approved, ordered, received, transferred, closed.
- `EvidenceStatus`: parsed email only, human confirmed, SAP confirmed, conflicting evidence.

This avoids treating “closed” as both dispatched and replenished. In the sample threads, a part can be dispatched quickly while replenishment and faulty return continue separately.

## Parsing Improvements

- Ask GPT to return line-level JSON with stable identifiers per line item: call number, MS conversation id, part number, quantity, customer/site, delivery city/address, serial, SLA, PO, order confirmation, and evidence text.
- Include an explicit `MessageType` classification: initial order, acknowledgement, delivery note, PO approval, Fujitsu order confirmation, collection/return, cancellation, general update.
- Require the model to return `null` rather than guessing when a value is not present.
- Store the source email timestamp and sender for every parsed field update.
- Reject updates where the model changes core identifiers, such as call number or part number, unless the email explicitly says it is a correction.

## Matching Improvements For Planning V2

- Use `MSConvoID + CallNumber + line part number` as the primary ticket-line identity.
- Use PO enrichment only when the PO number is explicit and the SAP PO line part either matches the ticket part exactly or maps to the same `SPLMaster`.
- Current ticket PO values are stored as attachment names, for example `26PO000031.pdf`; the Planning import should use the filename stem, for example `26PO000031`, only as reconciliation evidence against SAP `OPOR.DocNum`. If SAP does not contain the same PO number, flag it as a data-quality issue rather than replacing the SAP PO source.
- Keep alternative-part cases visible: requested, dispatched, and replenished parts may differ even when they belong to the same planning master.
- Add a review queue for unmatched PO/order-confirmation emails instead of applying fuzzy matching automatically.

## Operational Quick Wins

- Add a daily exception report for tickets with no `SPLMaster`, no delivery note after dispatch, replenishment approved but no Fujitsu confirmation, or expected faulty return overdue.
- Add a human confirmation step when GPT parses multiple parts from one email thread.
- Keep the original email body/link on the ticket so ambiguous lifecycle updates can be reviewed without hunting in Outlook.
