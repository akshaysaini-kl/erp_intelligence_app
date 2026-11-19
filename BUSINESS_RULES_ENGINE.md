# Business Rules Engine

A generic engine that tells a caller whether an operation on an ERP document may
proceed, before the caller performs it. The Purchase Order / Create rule set is the
first pilot.

The engine only reads. It never creates, saves, submits, cancels or deletes a
document, and never changes `docstatus` or `workflow_state`. ERPNext permissions,
validations and workflows stay authoritative. Creating the document is the job of the
caller's authorized execution layer. Every report says `"execution_performed": false`.

## Parts

| Part | Where |
|---|---|
| DocTypes: Business Rule Set, Business Rule, Business Rule Assignment | `erp_intelligence/erp_intelligence/doctype/` |
| Engine | `services/business_rules/engine.py` |
| Evaluator registry | `services/business_rules/registry.py` |
| Result contract | `services/business_rules/results.py`, `constants.py` |
| Read-only data access (uses ERP Intelligence) | `services/business_rules/data_access.py` |
| Purchase Order evaluators | `services/business_rules/evaluators/purchase_order.py` |
| Pilot rule set definition and seed | `services/business_rules/pilots/purchase_order_create.py` |
| Seed patch | `patches/seed_purchase_order_create_rule_set.py` (listed in `patches.txt`) |
| Tests | `tests/test_business_rules_*.py` |

## DocTypes

* **Business Rule Set**: `rule_set_name`, `target_doctype`, `operation`, `enabled`,
  `version`, `description`, and a table `rules` of Business Rule Assignment. At most one
  enabled set may exist per target DocType and operation (enforced on save).
* **Business Rule**: `rule_code` (unique, upper case, also the record name), `rule_name`,
  `description`, `rule_category`, `evaluator_key`, `execution_stage`, `default_severity`,
  `is_blocking`, `enabled`, `default_parameters` (JSON object).
* **Business Rule Assignment** (child table): `business_rule`, `sequence`, `enabled`,
  `parameters_override` (JSON object), `severity_override`.

A rule is reusable: assign it to as many sets as you like. Each assignment can override
parameters and severity for that set only.

Rule configuration is read with `frappe.get_all` / `frappe.get_doc`, so running the engine
does not need read permission on the three DocTypes. Editing them is limited to System
Manager. ERP business data is never read this way (see Data access).

## How a run works

```python
from erp_intelligence.services.business_rules.engine import BusinessRulesEngine

report = BusinessRulesEngine.evaluate(
    "Purchase Order", "Create", payload,
    stage="PRE_EXECUTION",            # PRE_EXECUTION | PRE_COMMIT | POST_EXECUTION
    confirmations=["exchange_rate_confirmed"],   # keys the user has explicitly confirmed
    execution_options={"submit": False},         # the flags the caller intends to use
    execution_result={"name": "PO-26-27-000009"},  # POST_EXECUTION only
)
if not report["can_proceed"]:
    ...  # report["decision"], report["pending_inputs"], report["pending_confirmations"] say why
```

1. Resolve the enabled Business Rule Set for target DocType and operation.
2. Take its enabled assignments in `sequence` order (equal sequences keep row order).
3. Keep the rules whose `execution_stage` equals the requested stage.
4. Per rule: effective parameters = rule `default_parameters` with the assignment's
   `parameters_override` merged on top (objects merge, lists and scalars replace);
   effective severity = `severity_override` or `default_severity`.
5. Look up `evaluator_key` in the registry and call the evaluator with an `EvaluationContext`.
6. Validate the returned result against the contract.
7. Combine the results into a report and a decision.

Rules that are skipped (disabled rule, disabled assignment, other stage) are listed in
`report["skipped"]`.

### Stages

`PRE_EXECUTION` checks the inputs before the payload is built. `PRE_COMMIT` checks the
final payload just before the caller commits. `POST_EXECUTION` checks the outcome. Each
rule belongs to one stage. In the pilot, the tax, duplicate and forbidden-field rules run
at `PRE_COMMIT`, and the read-back runs at `POST_EXECUTION`.

### Decisions

Highest applicable first:

| decision | meaning | `can_proceed` |
|---|---|---|
| `ERROR` | a blocking rule could not be evaluated, or no usable rule set | no |
| `BLOCKED` | a blocking rule failed | no |
| `NEEDS_INPUT` | a blocking rule needs an input | no |
| `NEEDS_CONFIRMATION` | a blocking rule waits for a user confirmation | no |
| `PROCEED_WITH_WARNINGS` | no blocking problem, but warnings or non-blocking issues exist | yes |
| `PROCEED` | every evaluated rule passed | yes |

A non-blocking rule never stops the operation, but if it does not pass it is listed in
`non_blocking_issues` and the decision becomes `PROCEED_WITH_WARNINGS`. A missing or
disabled rule set, an ambiguous set, an invalid request and an empty set all return
`ERROR` with an `error_code`. The engine fails closed.

## Result contract

Every evaluator result has exactly these fields. The engine rejects anything else.

| field | notes |
|---|---|
| `rule_code` | must equal the rule being run |
| `status` | `PASS`, `FAIL`, `WARNING`, `NEEDS_INPUT`, `NEEDS_CONFIRMATION`, `ERROR` |
| `severity` | the effective severity of the assignment |
| `is_blocking` | the rule's flag |
| `message` | non-empty text for the user |
| `details` | JSON-safe object with the evidence |
| `required_inputs` | non-empty for `NEEDS_INPUT`, empty otherwise |
| `requires_confirmation` | true exactly for `NEEDS_CONFIRMATION` |

A `NEEDS_CONFIRMATION` result also lists `details.confirmation_keys`. The caller asks the
user, then re-runs with those keys in `confirmations`. A key counts only when the caller
passes it, and a mapping value must be exactly `True`. A confirmation is not tied to a
payload, so the caller must discard confirmations when the payload changes.

A business rule failure is `FAIL`. An evaluator that cannot run is `ERROR`, with
`details.error_type` set to one of: `unregistered_evaluator`, `invalid_configuration`,
`data_access`, `contract_violation`, `evaluator_exception`. Report-level ERP Intelligence
read problems (permission denied, unresolved field, incomplete page) are `ERROR`, never `PASS`.

Status vocabulary used by the pilot evaluators:

* `FAIL`: a supplied value violates the rule.
* `NEEDS_INPUT`: a value is missing or ambiguous, or an earlier dependency (such as an
  unresolved supplier) means the rule cannot be evaluated yet.
* `NEEDS_CONFIRMATION`: acceptable only if the user explicitly confirms.
* `WARNING`: allowed, but the user must be told.

## Data access

Evaluators receive `context.data`, a `RuleDataAccess`. It is the only way to read ERP data:

| Need | Call | Uses |
|---|---|---|
| records | `fetch_records`, `fetch_by_names` | `IntelligenceService.execute_request` |
| child rows | `fetch_child_records` | the same, with `parent_context` |
| counts | `count_records` | the same, with `aggregations` |
| field discovery | `available_fields`, `require_fields` | `MetadataService.get_doctype_metadata` |

Reads run as the session user through ERP Intelligence's planner, field policy and
`frappe.get_list`, so permissions apply. Pages that overflow raise a data error instead of
returning a partial list. Identical requests inside one run are answered once. Evaluators
must not call `frappe.db`, `get_list`, `get_all` or `get_doc` themselves; a test enforces this.

The ERP Intelligence filter grammar has no OR. Conditions such as "`valid_upto` empty or
not before today" are evaluated in Python on the returned rows.

## Add a new rule (existing evaluator)

1. Create a **Business Rule** (desk or code): a unique upper-case `rule_code`, the
   `evaluator_key` of a registered evaluator, a stage, severity, `is_blocking`, and
   `default_parameters` declaring every tunable parameter (use `null` for values that each
   site must configure).
2. Add it to a **Business Rule Set** as an assignment with a `sequence`. Put site values in
   `parameters_override`. An override may only use keys declared in `default_parameters`, so a
   typo is rejected on save (and reported as `ERROR` if it is stored by other means).
3. Run the rule set's tests.

## Add a new evaluator

1. Write a function of one argument, the `EvaluationContext`, that returns a standard result:

   ```python
   def supplier_has_email(context):
       state, name, _ = ...                       # read data only through context.data
       rows = context.data.fetch_records("Supplier", ["name", "email_id"], [("name", "=", name)])
       if not rows[0]["email_id"]:
           return context.failed("Supplier has no email address.", {"supplier": name})
       return context.passed("Supplier has an email address.")
   ```

   Use `context.passed`, `failed`, `warning`, `needs_input(message, [inputs])` and
   `needs_confirmation(message, [keys])`. Read parameters with `context.param(name)` or
   `context.required_param(name)` (an unset required parameter becomes an `ERROR`). Check
   confirmations with `context.is_confirmed(key)`. The payload, options and
   `execution_result` are copies, so mutating them has no effect on other rules.
   Raise nothing for a business failure; return `FAIL`. Raising reports `ERROR`.
2. Register it explicitly in your module's `register_all(registry)`:

   ```python
   EVALUATORS = {"supplier.has_email": supplier_has_email}

   def register_all(registry):
       for key, evaluator in EVALUATORS.items():
           registry.register(key, evaluator)
   ```

   Keys are lower-case dotted identifiers. Registering a key twice raises.
3. Make the module known to the default registry by importing it and calling its
   `register_all` in `registry.get_default_registry()`.
4. Create the Business Rule that points to the key.

Nothing stored in the database is ever imported or executed. `evaluator_key` is a lookup
string, and an unknown key gives an `ERROR` result.

For tests, build your own `EvaluatorRegistry` and pass `registry=` and `data_access=` to
`BusinessRulesEngine.evaluate`.

## Purchase Order pilot

Rule set `Purchase Order - Create` (Purchase Order / Create, enabled, version 1) has 15
atomic rules, each with its own evaluator. Source for each rule is
`PURCHASE_ORDER_CREATION_GUIDE.md`.

| Seq | Rule | Stage |
|---|---|---|
| 10 | PO_SUPPLIER_RESOLVES_ONE | PRE_EXECUTION |
| 20 | PO_SUPPLIER_ENABLED | PRE_EXECUTION |
| 30 | PO_SUPPLIER_NOT_ON_HOLD | PRE_EXECUTION |
| 40 | PO_SUPPLIER_PRICE_LIST_AVAILABLE | PRE_EXECUTION |
| 50 | PO_ITEMS_RESOLVE_ONE | PRE_EXECUTION |
| 60 | PO_ITEMS_ENABLED_PURCHASABLE | PRE_EXECUTION |
| 70 | PO_ITEMS_LINKED_TO_SUPPLIER | PRE_EXECUTION |
| 80 | PO_ITEMS_QUANTITY_UOM | PRE_EXECUTION |
| 90 | PO_ITEM_APPROVED_PRICE | PRE_EXECUTION |
| 100 | PO_CURRENCY_EXCHANGE_RATE | PRE_EXECUTION |
| 110 | PO_REQUIRED_BY_DATE_VALID | PRE_EXECUTION |
| 120 | PO_TAX_TREATMENT_RESOLVED | PRE_COMMIT |
| 130 | PO_DUPLICATE_GUARD | PRE_COMMIT |
| 140 | PO_FORBIDDEN_PAYLOAD_FIELDS | PRE_COMMIT |
| 150 | PO_POST_CREATION_READBACK | POST_EXECUTION |

The payload is the data the caller intends to send to the create call (`supplier`,
`company`, `currency`, `conversion_rate`, `buying_price_list`, `schedule_date`, `items`,
`taxes`, ...). Nothing is invented: a missing supplier, date, quantity, price, currency
or rate is `NEEDS_INPUT`, and the engine never fills it in.

Evidence and uncertainty from the guide are kept in `details.evidence` with the guide's tag
(`V` verified, `C` source code, `H` history, `U` unverified). For example, the supplier
hold rule carries `U` because ERPNext's enforcement of `on_hold` and `prevent_pos` was
never verified.

### Site configuration

Evaluators contain no site values. Site values are parameters:

* Rule defaults declare the parameters. Site-specific ones default to `null`, and an
  unconfigured rule returns `ERROR`.
* The pilot's `parameters_override` values (tax templates `Input GST ...`, workflow states,
  `custom_*` field names, state code `27`) come from the guide's verified `erp.localhost`
  configuration. They live in `pilots/purchase_order_create.py` and are written to the
  Rule Set assignments by the seed. For another site, edit the assignments on the Rule Set.

### Seeding

`seed_purchase_order_create_rule_set()` creates what is missing and leaves existing
records alone, so desk edits survive. `force=True` rewrites the pilot records. The patch
runs it on `bench migrate`. To run it by hand:

```
bench --site <site> execute erp_intelligence.services.business_rules.pilots.purchase_order_create.seed_purchase_order_create_rule_set
```

New DocTypes reach a site with `bench --site <site> migrate`, or for these three alone:
`bench --site <site> reload-doc erp_intelligence doctype <business_rule | business_rule_assignment | business_rule_set>`.

## Tests

```
bench --site <site> run-tests --app erp_intelligence --module erp_intelligence.tests.test_business_rules_engine
bench --site <site> run-tests --app erp_intelligence --module erp_intelligence.tests.test_business_rules_purchase_order
bench --site <site> run-tests --app erp_intelligence --module erp_intelligence.tests.test_business_rules_integration
```

`run-tests` needs `allow_tests` in the site config. The tests only roll back; they commit
nothing and never create a Purchase Order. They can also be run with plain `unittest`
after `frappe.init` and `frappe.connect`.

* `test_business_rules_engine`: DocTypes, rule set resolution, ordering, registry,
  parameters, the six statuses, blocking and non-blocking decisions, stages, safety.
* `test_business_rules_purchase_order`: the pilot rules and engine against controlled
  in-memory data that stands in for ERP Intelligence.
* `test_business_rules_integration`: the real ERP Intelligence services on site data,
  permissions, and static checks (no dynamic code, no writes, no site values).

## Not in this phase

State machines, SLAs, escalation, workflow replacement, an assistant tool that exposes the
engine, and any document creation.
