# Open Questions

1. Where are the Planning Tool v2 onboarding template files stored, and what are the exact required sheets/tables/columns?
2. Is CoCre8 onboarding limited to Fujitsu item groups 105-106, or should PlanningV2 support all customer/item groups?
3. Should PlanningV2 reuse the same SAP SQL query pattern, or are there approved Service Layer views/queries for the new tool?
4. Which dimensions are mandatory for v2 import: customers, warehouses, item masters, pricing, usage history, supersession, suppliers, lead times, or service levels?
5. Are `SPLMaster`, `MasterKey`, `RowKey`, and `RowSig` still required in v2, or are there new unique-key rules?
6. Should historical usage continue to come from the stock audit report, or should it be extracted directly from SAP?
7. Which GitHub owner should host the remote repo: `SPLChase`, `ServicePartsLogistics`, or another organization?
