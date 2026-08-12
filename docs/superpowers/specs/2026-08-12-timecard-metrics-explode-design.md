# Timecard Metrics — single metric group, exploded to rows

**Date:** 2026-08-12
**Component:** `keboola.ukg-wfm` (`ex-ukg-wfm`)
**Status:** Approved design, pre-implementation
**Stacked on:** the End Date fix branch (`c/end-date-param-failure-a8a354`, PR #2). Ships as its own PR.
**Nature:** Breaking output-schema change for `timekeeping_timecard_metrics`. Pre-GA (one customer in final testing).

## Problem

`timekeeping_timecard_metrics` emits each selected metric section as a single opaque JSON-string
column. `flatten_record` recurses into dicts but `json.dumps`-es any list (`transform.py:12`), so a
metric section (a list of per-day line items) collapses into one string. The row is keyed on
`employeeId_id`, so the output is **one row per employee** carrying a blob such as:

```
actualTotals,employeeId_id,employeeId_name,employeeId_qualifier
"[{""uniqueId"": ""14212:2026-07-27:409"", ""payCode"": {""id"": 409}, ""hoursAmount"": 8.0, ""applyDate"": ""2026-07-27"", ...}]",14212,REDACTED,REDACTED
```

Consequences: no date at the row level, no per-line-item columns, and an incremental load upserts
one blob per employee (overwriting the whole period). The data is effectively unqueryable and not
usefully incremental.

## Ground truth (from the ACTUAL_TOTALS cassette)

With a single metric select the API returns a root-level list, **one entry per employee**, each
entry containing exactly `employeeId` plus that one section list:

```json
[{"employeeId": {"id": 14212, "qualifier": "…", "name": "…"},
  "actualTotals": [
    {"uniqueId": "14212:2026-07-27:409", "employee": {"id": 14212, …},
     "payCode": {"id": 409, …}, "hoursAmount": 8.0, "wages": 0.0,
     "daysAmount": 0.0, "applyDate": "2026-07-27", "job": {"id": 9765, …},
     "amountType": "HOUR", "payPeriodWeek": 1, "payPeriodNumber": 16,
     "laborCategories": {…}}
  ]}]
```

- Each line item carries `uniqueId` = `employeeId:applyDate:payCode` (e.g. `14212:2026-07-27:409`) —
  a natural per-line-item key — plus `applyDate`, `hoursAmount`, `payCode`, `job`, etc.
- Some employees have an **empty** section list (they contribute no line items).

**Verified:** `uniqueId` is present on `actualTotals` line items.
**Not verified:** whether the other sections (scheduled/projected/contract totals, exception
counts, accrual summary, FTPT, …) also carry `uniqueId`. Only an Actual Totals recording exists.
The totals family very likely shares the shape; it is unconfirmed against live responses.

## Goal

Make `timekeeping_timecard_metrics` emit **one row per line item** of a **single** chosen metric
section, with the line-item fields promoted to columns (including `applyDate`) and a
component-determined primary key. No per-metric configuration by the user beyond picking the one
section.

Non-goals: table-per-metric; deep (multi-level) explode of nested lists inside a line item;
changing accruals; changing the windowing model.

## Design

### 1. Config / UI — single-select metric group

- Replace `metric_groups` (`type: array`, multi-select) in `component_config/configRowSchema.json`
  with `metric_group` (`type: string`, `format: select`), keeping the **same** enum values and
  `enum_titles` and the same `dependencies.resource = ["timekeeping_timecard_metrics"]`. The full
  enum list is unchanged (no remapping of the ~28 tokens).
- `configuration.py`: add `metric_group: str | None`. `effective_select` returns `[metric_group]`
  when set. **Back-compat fold:** if `metric_group` is empty but a legacy `metric_groups` list is
  present, use its first element, so the one existing config keeps running.
- **Required for this resource:** an empty `metric_group` on `timekeeping_timecard_metrics` fails
  fast with a clear UserException. Rationale: an empty select makes the API return *all* sections,
  which cannot be cleanly exploded into one table.

### 2. Explode transform (`src/client/transform.py`)

Add:

```python
def explode_record(record: dict) -> Iterator[dict]:
    """Expand one metric entry into one row per line item of its single list section.

    Split the entry into non-list fields (e.g. employeeId) and the one list-valued section.
    Yield {**flatten(non_list_fields), **flatten(item)} per list item. If the entry has no
    list section, yield the single flattened entry (graceful fallback).
    """
```

- One-level explode only. A nested list inside a line item stays a JSON string (via the existing
  `flatten_record`). No per-section logic.
- Each row is self-contained: the entry's `employeeId` object is flattened onto every exploded row
  (`employeeId_id`, …) alongside the line item's own fields.
- With single-select there is exactly one list section per entry; if more than one list is ever
  present, explode the first and leave the others as flattened JSON (defensive; not expected).

`flatten_record` itself is unchanged.

### 3. Resource registry (`src/client/resources.py`)

- Add `explode: bool = False` to `ResourceDef`.
- `timekeeping_timecard_metrics`: set `explode=True`; drop the static `primary_key=["employeeId_id"]`
  (PK becomes dynamic — see §4).
- Windowing unchanged: still `window_chunkable == False` (rollup, never date-split); memory lever
  stays `batch_size`. The explode is client-side on the response and does not change the fetch.
- Accruals and every other resource are untouched (`explode` defaults False).

### 4. Primary key / incremental — component-determined

In the write path (`component.py._stream_and_write_table`), once the exploded columns are known:

```
primary_key = ["uniqueId"] if "uniqueId" in seen_columns
              else (config primary_key or [])
```

- Incremental upsert engages only with a PK (existing `effective_incremental`). So Actual Totals
  (and any section that carries `uniqueId`) upsert incrementally with a real `applyDate` column; a
  section without `uniqueId` degrades to full-replace unless the user sets a row-level `primary_key`.
- The row-level `primary_key` override still works and wins when `uniqueId` is absent.
- The sticky-column / native-typed-schema / empty-table machinery is reused unchanged.

### 5. Streaming integration (`src/component.py`)

- `_stream_and_write_table` calls `explode_record` per API record when `resource.explode` is True,
  flattening+writing each yielded row (instead of `flatten_record` on the record directly).
- PK resolution moves to the dynamic rule above for exploded resources; `effective_primary_key` /
  `effective_incremental` continue to govern non-exploded resources exactly as today.

## Behavior notes

- Employees with an empty section list produce **zero** rows.
- A run where every employee's section is empty writes a **header-only** table (replace-to-empty on
  full load), via the existing `_write_empty_table` path.
- Output columns become the union of the line-item fields (e.g. `uniqueId`, `employeeId_id`,
  `payCode_id`, `hoursAmount`, `applyDate`, `wages`, `job_id`, `amountType`, `payPeriodWeek`,
  `payPeriodNumber`, `laborCategories_*`, …) — deterministic, sorted, native-typed as today.

## Tests

- **Functional 32** (`32_timecard_metrics_selected_actual_totals`): change config to
  `metric_group: "ACTUAL_TOTALS"`; regenerate `expected/` to the exploded shape **from the existing
  cassette** — no re-record needed (the change is client-side).
- **Functional 08** (`08_timekeeping_timecard_metrics`, currently all-sections): rework to a single
  metric group (all-sections is incompatible with single-select + explode).
- **New negative case:** empty `metric_group` on `timekeeping_timecard_metrics` → fail fast.
- **Mock test:** update the timecard-metrics fixture to the exploded shape.
- **Unit:** cover `explode_record` (list section → N rows; empty section → 0 rows; no-list entry →
  1 row) and the dynamic-PK selection.
- Run `uv run pytest -q` and `uv run ruff check src/ tests/` green.

## Docs / memory

- README: update the metrics field name (`metric_group`) and describe the exploded, per-line-item
  output with `uniqueId`/`applyDate`.
- Update the rollup memory note if the "one row per employee" phrasing needs a caveat (fetch is
  still a rollup; output is now per-line-item).

## Risks

- **Breaking output schema** for `timekeeping_timecard_metrics` — acceptable pre-GA, shipped as its
  own PR with the change called out; the customer re-selects the single metric group.
- **PK coverage of non-actualTotals sections** is unverified. The dynamic rule fails safe
  (full-replace) rather than emitting broken rows; hard-coding `uniqueId` as the registry PK is
  deferred until the other selects are probed live.
