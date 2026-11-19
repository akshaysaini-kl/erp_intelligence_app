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
| `ERROR` | any rule could not be evaluated (blocking or not), or no usable rule set | no |
| `BLOCKED` | a blocking rule failed | no |
| `NEEDS_INPUT` | a blocking rule needs an input | no |
| `NEEDS_CONFIRMATION` | a blocking rule waits for a user confirmation | no |
| `PROCEED_WITH_WARNINGS` | no blocking problem, but warnings or non-blocking issues exist | yes |
| `PROCEED` | every evaluated rule passed | yes |

A non-blocking rule never stops the operation on a business outcome: if it returns `FAIL`,
`NEEDS_INPUT` or `NEEDS_CONFIRMATION` it is listed in `non_blocking_issues` and the decision
becomes `PROCEED_WITH_WARNINGS`. An evaluator failure is different. Any `ERROR` result,
whatever the rule's `is_blocking` flag or severity, gives `decision` `ERROR` and
`can_proceed` false: `is_blocking` says what a failed check means for the operation, and
cannot say that an unevaluated rule may be ignored. A missing or disabled rule set, an
ambiguous set, an invalid request and an empty set also return `ERROR` with an `error_code`.
The engine fails closed.

A rule set with no enabled rule for the requested stage is also an `ERROR`, with
`error_code` `NO_RULES_FOR_STAGE`, `can_proceed` false and no results. Nothing was checked, and
`PROCEED` would let a caller read "unchecked" as "allowed". The codes stay distinct:
`RULE_SET_NOT_FOUND` (no set for the DocType and operation), `RULE_SET_DISABLED`,
`RULE_SET_AMBIGUOUS`, `RULE_SET_EMPTY` (the set has no enabled assignment at all) and
`NO_RULES_FOR_STAGE` (the set has assignments, but none is an enabled rule for this stage). The
message names the stages that do have rules, and `skipped` lists the other-stage rules. At
`POST_EXECUTION` the report's `outcome_verification` is `UNVERIFIED`. A rule set that has rules
for only some stages therefore returns `ERROR` when the caller evaluates a stage it does not
cover; the caller should evaluate only stages the rule set covers.

The report keeps business outcomes and evaluator failures apart:

* `warnings` and `non_blocking_issues` hold business outcomes only. They never contain an `ERROR`.
* `evaluation_errors` lists every `ERROR`: `[{rule_code, error_type, rule_is_blocking}]`, with
  `error_type` one of `unregistered_evaluator`, `invalid_configuration`, `data_access`,
  `contract_violation`, `evaluator_exception`. An evaluator that returns the `ERROR` status
  itself is reported as `evaluator_exception`. `blocking_errors` lists the same rule codes
  (kept for existing consumers) and `summary.evaluation_errors` counts them.
* `outcome_verification` is `null` except at `POST_EXECUTION`, where it is `VERIFIED` (the
  checks ran and passed), `FAILED` (the checks ran and a rule failed) or `UNVERIFIED` (an
  evaluation error, missing input, pending confirmation or no usable rule set). `UNVERIFIED`
  means the caller must not assume the operation succeeded or failed.

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
passes it, and a mapping value must be exactly `True`. See "Confirmation binding" for how
confirmations are tied to the request they were given for.

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

## Confirmation binding

A confirmation key such as `exchange_rate_confirmed` does not say which value was confirmed.
Every report that evaluated rules therefore carries `confirmation_binding`:

```json
{"version": 1, "available": true, "fingerprint": "v1.<32 hex>", "covers": ["version", "user", ...],
 "required": true, "confirmations_applied": ["k1"], "confirmations_rejected": null}
```

The fingerprint is `v1.` plus the first 128 bits of `HMAC-SHA256(key, canonical_json(context))`
(`services/business_rules/binding.py`). The context is the binding version, the session user,
DocType, operation, stage, the rule set (name, version, modified), the modified timestamp of
every rule that is evaluated, the payload, `execution_options` and `execution_result`. It does
not include ERP evidence read during the run (supplier status, prices), so the caller must
evaluate again at `PRE_COMMIT` with current data.

* **Key.** Derived as `HMAC-SHA256(encryption_key, "erp_intelligence.business_rules.confirmation_binding.v1")`,
  reading `frappe.conf["encryption_key"]`. The key is never created or written
  (`get_encryption_key()` can write to the site configuration, so it is not used). With no
  usable key the binding is `available: false` and no confirmation is ever applied.
* **Canonical form.** Compact UTF-8 JSON, keys sorted at every depth, list order kept. `1`, `1.0` and
  `Decimal("1.00")` are the same number. Strings are not numbers, booleans are not numbers,
  `None` is not a missing key, dates are ISO strings, and no Unicode normalization is applied.
  Golden vectors in `tests/test_business_rules_binding.py` pin the format. Changing it
  invalidates every outstanding confirmation, so bump `BINDING_VERSION` instead.
* **Behaviour.** The assistant tool always requires the binding. Confirmations are applied only
  when `confirmation_binding` equals the fingerprint of the current request (compared with
  `hmac.compare_digest`). Otherwise all of them are discarded, the decision stays
  `NEEDS_CONFIRMATION`, and `confirmations_rejected` gives `{keys, reason}` with reason
  `binding_missing`, `binding_invalid` (malformed), `binding_stale` (well formed, but the request
  or its context changed) or `binding_unavailable`. Python callers of `BusinessRulesEngine.evaluate`
  are unchanged: without `require_confirmation_binding` they need no binding, but a binding
  they do supply must match.
* **What it does not do.** The fingerprint is keyed, so it reveals nothing about the payload and
  nothing is stored. It shows that confirmations were returned for the same request. It cannot
  show that the user agreed: the caller sits between the user and this code.

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

## Assistant tool: `evaluate_business_rules`

`erp_intelligence/tools/evaluate_business_rules.py` exposes the engine to FAC as an MCP
tool. It is a thin adapter: it validates the request, checks that the session user may
read the DocType, calls `BusinessRulesEngine.evaluate` once, and returns the engine's
report unchanged. It does not evaluate rules, read rule configuration or read ERP data
itself, and it never writes a document. The tool description that agents read is generic
and names no DocType or process.

| Argument | |
|---|---|
| `doctype`, `operation`, `payload` | required. `operation` is Create, Update, Submit, Cancel or Delete |
| `stage` | optional, default `PRE_EXECUTION`. One stage is evaluated per call |
| `confirmations` | optional list of confirmation keys the user has approved. Applied only with `confirmation_binding` |
| `confirmation_binding` | optional, required whenever `confirmations` is not empty: `confirmation_binding.fingerprint` from the report that asked |
| `execution_options`, `execution_result` | optional objects (`execution_result` is for `POST_EXECUTION`) |
| `user` | optional assertion. It must equal the session user |

Flow: the tool switch, then argument validation, then the user assertion, then
`frappe.has_permission(doctype, "read")`, then the engine, then a consistency check of the
report. Every response is a report with `execution_performed: false`. The tool does not
raise for its own failures. It returns `decision` ERROR with `can_proceed` false and one of
these `error_code` values: `TOOL_DISABLED`, `INVALID_REQUEST`, `PERMISSION_DENIED`
(also used for a DocType that does not exist, so the response does not reveal which
DocTypes exist) and `EVALUATION_FAILED` (an unexpected exception, or an engine report that
contradicts itself, for example `can_proceed` true next to a failed blocking rule). The
engine's own codes (`RULE_SET_NOT_FOUND`, `RULE_SET_DISABLED`, `RULE_SET_AMBIGUOUS`,
`RULE_SET_EMPTY`, `NO_RULES_FOR_STAGE`) pass through. The exception text is logged, not returned.

One thing FAC does before the tool runs: it checks that `doctype`, `operation` and
`payload` are present and of the right type. A failure there is a FAC error, not a report.

Authentication and authorization stay with FAC and Frappe. There is no second mechanism.
Evidence is read through ERP Intelligence as the session user.

### Disabled by default

Two independent layers keep the tool unavailable to agents until it is enabled on purpose:

1. **A disabled FAC Tool Configuration row.** FAC treats a tool with no row as enabled for
   everyone, and its migrate sync creates a missing row with `enabled = 1`.
   `utils/fac_tool_configuration.py` creates the row first with `enabled = 0`, from the
   `before_migrate` and `after_install` hooks. A patch alone is not enough: `bench install-app`
   marks all patches as already run, and the next migrate would then let FAC create an
   enabled row. The helper never edits an existing row, so an administrator's choice
   survives. A disabled row hides the tool from `tools/list` and makes `tools/call` refuse it.
2. **The tool's own switch.** `execute` returns `TOOL_DISABLED` unless the site configuration
   contains the JSON value `true`:

   ```json
   "assistant_tools": {"evaluate_business_rules": {"enabled": true}}
   ```

   Any other value, or a configuration that cannot be read, leaves it off. This layer is
   needed because FAC fails open: with no row, or when it cannot read its configuration, it
   lists every tool.

Between deploying this code and the next `bench migrate` there is no FAC row, so the tool
is listed, but every call returns `TOOL_DISABLED`.

To enable it: set the site configuration above, then in FAC admin enable the
`evaluate_business_rules` Tool Configuration (and restrict roles if wanted). The generic
agent prompt is not changed by this tool.

### Audit log and logging

FAC's `BaseTool.log_execution` writes every call to `Assistant Audit Log` and passes the
arguments and the output through `_sanitize_arguments` and `_sanitize_data`. FAC's versions
redact only top-level argument keys (a nested payload is stored in full), cut lists longer
than 10 to 3 entries, and redact any key containing `auth` or `token`. The tool overrides both
(FAC is not modified) and stores only:

* input: `doctype`, `operation`, `stage`, `{size_bytes, key_count}` for the payload, the supplied
  `confirmation_binding` if it is well formed, and the confirmation keys (a key with a `:` keeps
  only the identifier before it, because rules may put a document value after the colon);
* output: `decision`, `can_proceed`, `error_code`, `rule_set` name and version, summary counts,
  and `{rule_code, status, is_blocking, severity}` for every result.

Messages, `details`, evidence, `execution_options` and `execution_result` are not stored. If a
projection cannot be built, nothing from the original is stored. Tests check the override, that
FAC's audit path still calls the overridden methods, and the stored row end to end. If a FAC
upgrade renames those methods, the static test fails.

The tool and the engine log exceptions by class and location only. Exception messages are left
out because they can contain document values.

FAC itself can write tool arguments outside this path. See the investigation notes below.

### FAC logging outside the audit log (investigated, FAC not modified)

Findings from reading FAC 2.3.1 and Frappe 15.100 and from this bench's configuration. They
describe what the code does, not a clean bill of health for any deployment.

| Path | What is logged | Condition |
|---|---|---|
| Legacy endpoint `api/assistant_api.handle_assistant_request` (`allow_guest=True`, POST), lines 84 and 94, and `api/handlers/tools.py:75` | the whole request body and the `tools/call` params, including the arguments, at DEBUG through `api_logger` | `api_logger` is a stdlib logger with a stderr `StreamHandler`. Its level is DEBUG when `frappe.conf["developer_mode"]` is truthy at the time the logger is first created in the process, otherwise INFO. Frappe's log level does not apply to it |
| MCP endpoint `api/fac_endpoint.handle_mcp` -> `mcp/server.py:284` | tool name and the full arguments, at DEBUG through `frappe.logger()` (`logs/frappe.log`) | Frappe's default level is WARNING when `DEV_SERVER` is set and ERROR otherwise. Only `bench console` sets `frappe.log_level` (INFO, that process only); nothing else in FAC or Frappe calls `set_log_level`. `logs/frappe.log` on this bench holds no such line |
| `mcp/server.py:161` | the first 500 characters of the raw request body | ERROR, only when the body is not valid JSON, so it is always written |
| `AssistantLogger.error` (`api_logger`) | the message, also written to the Error Log table (cleared after 14 days) | ERROR calls in FAC include the text of exceptions (`str(e)`), never the arguments |
| `BaseTool._safe_execute`, general exception branch | the raw arguments (`Args: ...`) in an Error Log row and the traceback in `logs/<module>.log` | only when `execute()` raises an `Exception`. `EvaluateBusinessRules.execute` catches every `Exception`, so it does not reach this branch |

Responses are not logged by any of these paths. For a JSON request, `Form Dict` in the
`frappe.logger()` lines was `{}` in the entry I inspected, so the body is not copied there.

What this means for exposure of tool-call arguments:

* With `developer_mode` off and Frappe's default log level, I found no path that writes the
  arguments of a successful `tools/call` to a log or to the database apart from the audit
  projection above.
* With `developer_mode` on, the legacy endpoint writes the full arguments, including the payload,
  to the stderr of the web process. This bench has `developer_mode: 1` in
  `sites/common_site_config.json`, so it applies here if clients use the legacy endpoint. Which
  endpoint clients use, and where the web process's stderr goes (terminal, supervisor, journal),
  is not visible from the code and was not verified.
* The MCP endpoint's debug line appears only if something raises the Frappe log level to DEBUG.
* A malformed JSON body is logged in part at ERROR on the MCP endpoint, whatever the settings.
* Not checked: reverse proxy, load balancer and hosting-platform request logging. That is outside
  this repository.

Before enabling the tool on a production site, confirm that `developer_mode` is off, that
`frappe.log_level` is not raised, and where the web process's stderr is kept.

### Known limitations

* The binding does not cover ERP data read during evaluation, and it cannot show that the user
  agreed. See "Confirmation binding".
* A decision applies only to the payload, stage and data state that were evaluated.
* The tool's audit rows are kept as long as FAC keeps `Assistant Audit Log`, which has no
  automatic clearing configured. FAC also does not store failed executions (its Status field has
  no "Failed" option, so those inserts are rejected and swallowed), and a refusal by this tool is
  a successful execution that returns an `ERROR` report.

## Tests

```
bench --site <site> run-tests --app erp_intelligence --module erp_intelligence.tests.test_business_rules_tool
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
* `test_business_rules_tool`: the `evaluate_business_rules` tool: registration and schema,
  description and schema consistency, engine invocation, permissions, fail-closed paths, the
  strict error policy, audit projection, malformed input, no ERP writes, the disabled FAC row,
  and FAC's behaviour with and without it.
* `test_business_rules_binding`: canonical form, golden vectors, key derivation, the engine's
  handling of confirmations, and the tool's checks on the binding section.

## Not in this phase

State machines, SLAs, escalation, workflow replacement, payload-bound confirmations, the
generic agent prompt, and any document creation.
