# Timecard Metrics Explode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `timekeeping_timecard_metrics` take a single metric group and emit one row per line item of that section, keyed on the API's `uniqueId`, instead of one opaque JSON blob per employee.

**Architecture:** A new pure `explode_record` transform expands each per-employee metric entry into per-line-item dicts; the streaming writer applies it when a resource has a new `explode` flag. The primary key is resolved dynamically at write time (`uniqueId` when the exploded rows carry it, else the user PK, else keyless). The metric picker becomes a single-select `metric_group` config field, required for this resource.

**Tech Stack:** Python 3.12 (Keboola component), pydantic config model, `keboola.component` output tables + native-type manifest, pytest, ruff, ty. Package manager: `uv`.

## Global Constraints

- Run `uv run pytest -q` and `uv run ruff check src/ tests/` — both green before each commit.
- Type-checks with `ty` (pre-commit hook runs it); keep type annotations complete.
- Branch: `c/timecard-metrics-explode-rows`, stacked on the End Date branch (`c/end-date-param-failure-a8a354`). This is a **breaking output-schema change** shipped as its own PR.
- Never put customer/project/stack names in commits, code, tests, or docs.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- `flatten_record` is **unchanged**. The fetch/windowing model is **unchanged** (`timekeeping_timecard_metrics` stays a rollup: `window_chunkable == False`, never date-split, memory via `batch_size`).
- The metric-group enum (29 tokens) and its titles are **unchanged** — only the field cardinality (multi → single) changes.
- The natural per-line-item key is `uniqueId` (format `employeeId:applyDate:payCode`, e.g. `14212:2026-07-27:409`), verified present on `actualTotals`; other sections are unverified, so PK resolution must degrade gracefully.

---

### Task 1: `explode_record` transform

**Files:**
- Modify: `src/client/transform.py`
- Test: `tests/unit/test_transform.py`

**Interfaces:**
- Consumes: nothing (pure function over a dict).
- Produces: `explode_record(record: dict[str, Any]) -> Iterator[dict[str, Any]]` — yields one dict per line item of the record's single list section, merged with the record's non-list fields; yields nothing when the section is empty **or absent** (an employee with no line items produces no rows). Callers still apply `flatten_record` to each yielded dict. (As-built: an earlier draft of this task yielded the record unchanged for the no-list-section case; it was corrected to zero rows during implementation — `src/client/transform.py` is the source of truth.)

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_transform.py`:

```python
from client.transform import explode_record, flatten_record


def test_explode_one_row_per_line_item():
    entry = {
        "employeeId": {"id": 14212},
        "actualTotals": [
            {"uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
            {"uniqueId": "14212:2026-07-26:801", "applyDate": "2026-07-26", "hoursAmount": 6.0},
        ],
    }
    rows = [flatten_record(r) for r in explode_record(entry)]
    assert rows == [
        {"employeeId_id": 14212, "uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
        {"employeeId_id": 14212, "uniqueId": "14212:2026-07-26:801", "applyDate": "2026-07-26", "hoursAmount": 6.0},
    ]


def test_explode_empty_section_yields_no_rows():
    assert list(explode_record({"employeeId": {"id": 67127}, "actualTotals": []})) == []


def test_explode_no_list_section_yields_single_entry():
    assert list(explode_record({"employeeId": {"id": 1}})) == [{"employeeId": {"id": 1}}]


def test_explode_scalar_list_items_wrap_under_section_key():
    entry = {"employeeId": {"id": 1}, "vals": [10, 20]}
    assert list(explode_record(entry)) == [
        {"employeeId": {"id": 1}, "vals": 10},
        {"employeeId": {"id": 1}, "vals": 20},
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_transform.py -v`
Expected: FAIL with `ImportError: cannot import name 'explode_record'`.

- [ ] **Step 3: Implement `explode_record`**

In `src/client/transform.py`, add the import and the function (keep `flatten_record` as-is):

```python
from collections.abc import Iterator
```

```python
def explode_record(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Expand one metric entry into one row per line item of its single list section.

    A timecard_metrics entry (single metric select) is {"employeeId": {...}, "<section>": [items]}.
    Yield one dict per item, merged with the entry's non-list fields (employeeId) so every row is
    self-contained; callers still apply flatten_record. An entry with no list section yields the
    single entry unchanged (fallback); an empty section yields no rows. Only the FIRST list section
    is exploded — with a single metric select there is exactly one; any other list stays on the base
    dict for flatten_record to JSON-serialize.
    """
    list_keys = [key for key, value in record.items() if isinstance(value, list)]
    if not list_keys:
        yield record
        return
    section = list_keys[0]
    base = {key: value for key, value in record.items() if key != section}
    for item in record[section]:
        yield {**base, **item} if isinstance(item, dict) else {**base, section: item}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_transform.py -v`
Expected: PASS (all four new tests + the existing `test_flatten_nested_and_lists`).

- [ ] **Step 5: Lint + commit**

```bash
cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-ukg-wfm/.claude/worktrees/zen-williamson-72f6c9
uv run ruff check src/ tests/
git add src/client/transform.py tests/unit/test_transform.py
git commit -m "feat: explode_record transform for per-line-item metric rows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Single-select `metric_group` config field + schema

**Files:**
- Modify: `src/configuration.py:60-61` (field) and `src/configuration.py:104-114` (`effective_select`)
- Modify: `component_config/configRowSchema.json:209-289` (the `metric_groups` block)
- Test: `tests/unit/test_configuration.py:38-66` (replace the metric tests)

**Interfaces:**
- Consumes: nothing.
- Produces: `Configuration.metric_group: str | None`; `Configuration.effective_select` returns `[metric_group]` for `timekeeping_timecard_metrics` (folding a legacy `metric_groups` list to its first element), else the free-text `select`.

- [ ] **Step 1: Rewrite the failing tests**

Replace the current metric tests in `tests/unit/test_configuration.py` (the block spanning `test_effective_select_folds_metric_groups` through `test_empty_metric_groups_means_all_sections_ignoring_stale_select`, lines 38-66) with:

```python
def test_effective_select_uses_single_metric_group():
    cfg = Configuration(**_root(), resource="timekeeping_timecard_metrics", metric_group="ACTUAL_TOTALS")
    assert cfg.effective_select == ["ACTUAL_TOTALS"]


def test_legacy_metric_groups_folds_to_first_element():
    # Back-compat: a pre-explode config stored a list; use its first element.
    cfg = Configuration(
        **_root(),
        resource="timekeeping_timecard_metrics",
        metric_groups=["ACTUAL_TOTALS", "SCHEDULED_TOTALS"],
    )
    assert cfg.effective_select == ["ACTUAL_TOTALS"]


def test_metric_group_wins_over_stale_select():
    cfg = Configuration(
        **_root(),
        resource="timekeeping_timecard_metrics",
        select=["SCHEDULED_TOTALS"],  # stale/hidden leftover
        metric_group="ACTUAL_TOTALS",
    )
    assert cfg.effective_select == ["ACTUAL_TOTALS"]


def test_metric_group_wins_over_legacy_metric_groups():
    cfg = Configuration(
        **_root(),
        resource="timekeeping_timecard_metrics",
        metric_group="ACTUAL_TOTALS",
        metric_groups=["SCHEDULED_TOTALS"],
    )
    assert cfg.effective_select == ["ACTUAL_TOTALS"]


def test_empty_metric_group_yields_empty_select():
    cfg = Configuration(**_root(), resource="timekeeping_timecard_metrics")
    assert cfg.effective_select == []


def test_non_metrics_resource_uses_free_text_select():
    cfg = Configuration(**_root(), resource="persons", select=["FOO"], metric_group="ACTUAL_TOTALS")
    assert cfg.effective_select == ["FOO"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_configuration.py -v`
Expected: FAIL — `metric_group` is not yet a config field / `effective_select` doesn't fold it.

- [ ] **Step 3: Add the field**

In `src/configuration.py`, replace the current `metric_groups` field + comment (lines 59-61) with:

```python
    # Timecard-metrics-only picker: the ONE metric group (API `select` token) chosen from the static
    # single-select dropdown. Authoritative for timecard_metrics' effective select. `metric_groups`
    # is retained only to fold a pre-explode (multi-select) config to its first element; new configs
    # use metric_group.
    metric_group: str | None = None
    metric_groups: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Update `effective_select`**

Replace the body of `effective_select` (lines 104-114) with:

```python
    @computed_field
    @property
    def effective_select(self) -> list[str]:
        """API `select` groups. For timecard_metrics the single metric-group picker (`metric_group`)
        is authoritative; a stale/raw `select` must not override it, and a legacy multi-value
        `metric_groups` folds to its first element (back-compat). An empty picker yields an empty
        select — the component rejects that for timecard_metrics (it needs exactly one section to
        explode). Every other resource falls back to the free-text `select`.
        """
        if self.resource == "timekeeping_timecard_metrics":
            if self.metric_group:
                return [self.metric_group]
            if self.metric_groups:
                return [self.metric_groups[0]]
            return []
        return self.select
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_configuration.py -v`
Expected: PASS.

- [ ] **Step 6: Update the row schema to a single-select**

In `component_config/configRowSchema.json`, replace the entire `"metric_groups": { ... }` block (lines 209-289) with a single-select **string** field named `metric_group`. Copy the `enum` array (all 29 values) and the `enum_titles` array (all 29 titles) **verbatim** from the old block — do not add, remove, reorder, or reword any value/title:

```json
    "metric_group": {
      "type": "string",
      "title": "Timecard Metric Group",
      "format": "select",
      "description": "The one metric group (select token) to pull for Timekeeping — Timecard Metrics. Its line items are exploded into one row per item (with applyDate and a uniqueId primary key). Exactly one group is required for this resource.",
      "enum": [
        "FTPTDATA_ALL",
        "FTPTDATA",
        "AVERAGING",
        "SCHEDULED_TOTALS",
        "CONTRACT_TOTALS",
        "PROJECTED_TOTALS",
        "PROJECTED_TOTALS_ONLY_CORRECTIONS",
        "PROJECTED_TOTALS_EXCLUDE_CORRECTIONS",
        "ACTUAL_TOTALS",
        "ACTUAL_TOTALS_ONLY_CORRECTIONS",
        "ACTUAL_TOTALS_EXCLUDE_CORRECTIONS",
        "EXCEPTION_TOTAL",
        "EXCEPTION_TOTAL_UNREVIEWED",
        "EXCEPTION_TOTAL_EMPLOYEE_JUSTIFIED",
        "EXCEPTION_TOTAL_MANAGER_JUSTIFIED",
        "EXCEPTION_TOTAL_AUTO_RESOLVED",
        "SHIFT_ACTUAL_TOTAL_SUMMARY",
        "SHIFT_SCHEDULED_TOTAL_SUMMARY",
        "SHIFT_CONTRACT_TOTAL_SUMMARY",
        "SHIFT_PROJECTED_TOTAL_SUMMARY",
        "DAILY_ACTUAL_TOTAL_SUMMARY",
        "DAILY_SCHEDULED_TOTAL_SUMMARY",
        "DAILY_CONTRACT_TOTAL_SUMMARY",
        "DAILY_PROJECTED_TOTAL_SUMMARY",
        "ACCRUAL_SUMMARY",
        "ACCRUAL_TRANSACTIONS",
        "ABSENCE_EXCEPTION",
        "ISR_DAILY",
        "ISR_SUMMARY"
      ],
      "options": {
        "enum_titles": [
          "Full/part-time data — all weeks",
          "Full/part-time data — worked weeks",
          "Averaging totals",
          "Scheduled totals",
          "Contract totals",
          "Projected totals (incl. corrections)",
          "Projected totals — corrections only",
          "Projected totals — exclude corrections",
          "Actual totals",
          "Actual totals — corrections only",
          "Actual totals — exclude corrections",
          "Exception count — total",
          "Exception count — unreviewed",
          "Exception count — employee-justified",
          "Exception count — manager-justified",
          "Exception count — auto-resolved",
          "Shift summary — actual",
          "Shift summary — scheduled",
          "Shift summary — contract",
          "Shift summary — projected",
          "Daily summary — actual",
          "Daily summary — scheduled",
          "Daily summary — contract",
          "Daily summary — projected",
          "Accrual summary",
          "Accrual transactions",
          "Absence exceptions",
          "Include summary report — daily",
          "Include summary report — summary"
        ],
        "tooltip": "Pick the one metric group you need. Its response section is exploded into per-line-item rows (with applyDate and a uniqueId primary key). Leaving it unset is not allowed for this resource — an empty selection would return every section, which cannot be exploded into one table.",
        "dependencies": {
          "resource": ["timekeeping_timecard_metrics"]
        }
      },
      "propertyOrder": 61
    },
```

- [ ] **Step 7: Sanity-check the JSON parses + full unit run**

Run: `uv run python -c "import json; json.load(open('component_config/configRowSchema.json'))"`
Expected: no output (valid JSON).
Run: `uv run pytest tests/unit/test_configuration.py tests/unit/test_resources.py -q`
Expected: PASS (`test_resources.py` includes a schema gate that loads the row schema — it must still parse and its `resource` enum is untouched).

- [ ] **Step 8: Lint + commit**

```bash
uv run ruff check src/ tests/
git add src/configuration.py component_config/configRowSchema.json tests/unit/test_configuration.py
git commit -m "feat: single-select metric_group config field (folds legacy metric_groups)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Registry `explode` flag + `resolve_primary_key`

**Files:**
- Modify: `src/client/resources.py` (`ResourceDef` field; `timekeeping_timecard_metrics` entry; new `resolve_primary_key`)
- Test: `tests/unit/test_resources.py`

**Interfaces:**
- Consumes: `effective_primary_key` (existing).
- Produces: `ResourceDef.explode: bool` (default `False`); `resolve_primary_key(resource: ResourceDef, seen_columns: list[str], config_pk: list[str] | None = None) -> list[str]`. `timekeeping_timecard_metrics.explode is True` and its registry `primary_key` is `[]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_resources.py` (it already imports `get_resource`, `RESOURCE_REGISTRY`; add `resolve_primary_key` to the import from `client.resources`):

```python
def test_timecard_metrics_is_exploded_and_keyless_in_registry():
    r = get_resource("timekeeping_timecard_metrics")
    assert r.explode is True
    assert r.primary_key == []


def test_resolve_pk_exploded_prefers_uniqueid():
    r = get_resource("timekeeping_timecard_metrics")
    assert resolve_primary_key(r, ["uniqueId", "employeeId_id", "applyDate"]) == ["uniqueId"]


def test_resolve_pk_uniqueid_wins_over_config_for_exploded():
    r = get_resource("timekeeping_timecard_metrics")
    assert resolve_primary_key(r, ["uniqueId"], config_pk=["employeeId_id"]) == ["uniqueId"]


def test_resolve_pk_exploded_without_uniqueid_falls_back_to_config():
    r = get_resource("timekeeping_timecard_metrics")
    assert resolve_primary_key(r, ["employeeId_id", "applyDate"], config_pk=["employeeId_id"]) == ["employeeId_id"]


def test_resolve_pk_exploded_without_uniqueid_or_config_is_keyless():
    r = get_resource("timekeeping_timecard_metrics")
    assert resolve_primary_key(r, ["employeeId_id"]) == []


def test_resolve_pk_non_exploded_ignores_uniqueid():
    r = get_resource("persons")  # not exploded; registry PK ["personNumber"]
    assert resolve_primary_key(r, ["uniqueId", "personNumber"]) == ["personNumber"]
    assert resolve_primary_key(r, ["personNumber"], config_pk=["id"]) == ["id"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_resources.py -v -k "resolve_pk or exploded"`
Expected: FAIL — `resolve_primary_key` not importable / `explode` attribute missing.

- [ ] **Step 3: Add the `explode` field to `ResourceDef`**

In `src/client/resources.py`, add to `ResourceDef` (near `primary_key`, around line 109):

```python
    # When True, each API record is expanded into one row per line item of its single list section
    # (see transform.explode_record) instead of one row with the list JSON-serialized. Used by
    # timecard_metrics so the chosen metric section becomes per-line-item rows; the PK is then
    # resolved dynamically (resolve_primary_key) rather than from a static registry key.
    explode: bool = False
```

- [ ] **Step 4: Flip `timekeeping_timecard_metrics` to exploded + keyless**

In the `timekeeping_timecard_metrics` registry entry, set `explode=True` and change its `primary_key` from `["employeeId_id"]` to `[]`. Update the trailing comment on `primary_key` to note the PK is now the per-line-item `uniqueId`, resolved at write time. Concretely, in that entry:

```python
        records_key=None,
        explode=True,
        # Exploded to per-line-item rows; the PK is uniqueId when present (resolve_primary_key), so
        # the registry key is empty (dynamic). accruals keep employeeId_id — they are not exploded.
        primary_key=[],
```

- [ ] **Step 5: Add `resolve_primary_key`**

In `src/client/resources.py`, after `effective_primary_key`, add:

```python
# The natural per-line-item key exploded metric rows expose (employeeId:applyDate:payCode).
_EXPLODE_PK = "uniqueId"


def resolve_primary_key(
    resource: ResourceDef, seen_columns: list[str], config_pk: list[str] | None = None
) -> list[str]:
    """The output-table primary key, resolving the dynamic key for exploded resources.

    For an exploded resource the component owns the key: when the exploded rows expose `uniqueId`
    (the per-line-item natural key) it is the PK — even over a user/registry key — so sections that
    carry it (Actual/Scheduled/… totals) upsert incrementally with a real applyDate column. A
    section without `uniqueId`, and every non-exploded resource, falls back to effective_primary_key
    (user-supplied `primary_key` over the registry default).
    """
    if resource.explode and _EXPLODE_PK in seen_columns:
        return [_EXPLODE_PK]
    return effective_primary_key(resource, config_pk)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_resources.py -v`
Expected: PASS (new tests + existing registry tests; the accruals PK test at line 58 is unaffected — accruals are not exploded and keep `employeeId_id`).

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/ tests/
git add src/client/resources.py tests/unit/test_resources.py
git commit -m "feat: ResourceDef.explode + dynamic resolve_primary_key (uniqueId)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wire explode + dynamic PK + required-group guard into the component

**Files:**
- Modify: `src/component.py` — `run()` (guard), `_stream_and_write_table` (explode + dynamic PK), `_write_empty_table` (dynamic PK)
- Test: `tests/unit/test_compute_window.py` (guard), `tests/unit/test_manifest_schema.py` (exploded output)

**Interfaces:**
- Consumes: `explode_record` (Task 1), `resolve_primary_key` (Task 3), `Configuration.effective_select` (Task 2).
- Produces: exploded, `uniqueId`-keyed output for `timekeeping_timecard_metrics`; a fail-fast `UserException` when its `metric_group` is empty.

- [ ] **Step 1: Write the failing guard test**

Add to `tests/unit/test_compute_window.py`:

```python
def test_empty_metric_group_refused_for_timecard_metrics():
    # An exploded metrics resource needs exactly one section; an empty selection would return ALL
    # sections (not explodable into one table), so run() must fail fast before any API call.
    comp = _comp(resource="timekeeping_timecard_metrics", since="2026-01-01T00:00:00+00:00")
    with pytest.raises(UserException, match="metric group"):
        comp.run()


def test_metric_group_set_passes_the_guard(monkeypatch):
    # With a metric_group set, run() passes the guard and proceeds to fetch (which we stub to no-op).
    comp = _comp(
        resource="timekeeping_timecard_metrics",
        since="2026-01-01T00:00:00+00:00",
        metric_group="ACTUAL_TOTALS",
    )
    comp._record_source = lambda *a, **k: iter([])  # short-circuit before HTTP
    comp.run()  # must not raise
```

- [ ] **Step 2: Write the failing exploded-output test**

Add to `tests/unit/test_manifest_schema.py`:

```python
def test_timecard_metrics_explodes_to_line_items_keyed_on_uniqueid(tmp_path, monkeypatch):
    """Exploded metrics: one row per line item, keyed on uniqueId (non-nullable PK), applyDate
    promoted to a column; an employee with an empty section contributes no rows."""
    params = {
        **_PARAMS,
        "resource": "timekeeping_timecard_metrics",
        "load_type": "incremental_load",
        "metric_group": "ACTUAL_TOTALS",
    }
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("timekeeping_timecard_metrics")

    records = iter(
        [
            {
                "employeeId": {"id": 14212},
                "actualTotals": [
                    {"uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
                    {"uniqueId": "14212:2026-07-26:801", "applyDate": "2026-07-26", "hoursAmount": 6.0},
                ],
            },
            {"employeeId": {"id": 67127}, "actualTotals": []},  # empty section -> no rows
        ]
    )
    row_count, columns = component._stream_and_write_table(resource, records)

    assert row_count == 2
    manifest = _manifest(tmp_path / "data", "timekeeping_timecard_metrics")
    assert manifest["incremental"] is True
    cols = _schema_by_col(tmp_path / "data", "timekeeping_timecard_metrics")
    assert cols["uniqueId"]["primary_key"] is True
    assert cols["uniqueId"].get("nullable", False) is False
    assert {"uniqueId", "employeeId_id", "applyDate", "hoursAmount"} <= set(cols)
```

- [ ] **Step 3: Run both tests to verify they fail**

Run: `uv run pytest tests/unit/test_compute_window.py::test_empty_metric_group_refused_for_timecard_metrics tests/unit/test_manifest_schema.py::test_timecard_metrics_explodes_to_line_items_keyed_on_uniqueid -v`
Expected: FAIL — no guard yet; output is one blob row keyed on nothing/employeeId_id, not per-line-item on uniqueId.

- [ ] **Step 4: Add the required-group guard in `run()`**

In `src/component.py`, in `run()` immediately after `resource = get_resource(self._config.resource)` (line ~190), add:

```python
        # An exploded metrics resource needs exactly one section to explode. An empty selection
        # makes the API return every section (multiple lists per entry), which cannot be exploded
        # into one coherent table — fail fast before any request rather than emit a broken shape.
        if resource.explode and not self._config.effective_select:
            raise UserException(
                f"Resource '{resource.name}' requires exactly one metric group. Pick a single "
                "Timecard Metric Group so its line items can be exploded into rows."
            )
```

- [ ] **Step 5: Apply explode in the streaming phase**

In `_stream_and_write_table`, replace the phase-1 loop body that flattens each record. The current loop is:

```python
            for record in record_iter:
                row = flatten_record(record)
                for key in row:
                    seen_columns[key] = None
```

Change it so an exploded resource expands each record first. Add a small local helper just above the `with tempfile...` block:

```python
        def _rows(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
            if resource.explode:
                for exploded in explode_record(record):
                    yield flatten_record(exploded)
            else:
                yield flatten_record(record)
```

and rewrite the loop to iterate rows:

```python
            for record in record_iter:
                for row in _rows(record):
                    for key in row:
                        seen_columns[key] = None
                    if deferred_writer is None:
                        deferred_writer = csv.DictWriter(
                            tmp,
                            fieldnames=list(seen_columns),
                            extrasaction="ignore",
                            restval="",
                        )
                    elif set(row.keys()) - set(deferred_writer.fieldnames):
                        deferred_writer.fieldnames = list(seen_columns)
                    deferred_writer.writerow(row)
                    row_count += 1
```

Add `explode_record` to the import from `client.transform` (currently `from client.transform import flatten_record`):

```python
from client.transform import explode_record, flatten_record
```

- [ ] **Step 6: Resolve the PK dynamically in `_stream_and_write_table`**

In the phase-2 section, replace:

```python
            primary_key = effective_primary_key(resource, self._config.primary_key)
            is_incremental = self._effective_incremental(resource)
```

with:

```python
            # PK is resolved from the columns actually produced: an exploded resource keys on
            # uniqueId when present (resolve_primary_key), else the user/registry key. Incremental
            # upsert engages only with a non-empty PK.
            primary_key = resolve_primary_key(resource, list(seen_columns), self._config.primary_key)
            is_incremental = self._config.incremental and bool(primary_key)
```

Update the imports from `client.resources` to include `resolve_primary_key` (keep `effective_primary_key` — still used by `_write_empty_table`).

- [ ] **Step 7: Resolve the PK dynamically in `_write_empty_table`**

In `_write_empty_table`, replace:

```python
        primary_key = effective_primary_key(resource, self._config.primary_key)
        is_incremental = self._effective_incremental(resource)
```

with (resolve against the sticky columns so a prior uniqueId key is preserved on a zero-row run):

```python
        sticky = self._load_sticky_columns(resource)
        primary_key = resolve_primary_key(resource, sticky, self._config.primary_key)
        is_incremental = self._config.incremental and bool(primary_key)
```

Then reuse `sticky` for the existing `header_cols` line (remove the now-duplicate `sticky = self._load_sticky_columns(resource)` that follows). The `header_cols = sorted(set(primary_key) | set(sticky))` line stays.

- [ ] **Step 8: Run the targeted tests to verify they pass**

Run: `uv run pytest tests/unit/test_compute_window.py tests/unit/test_manifest_schema.py -v`
Expected: PASS, including the two new tests and the existing `test_full_load_backfills_sticky_columns_from_state` (its fake record has no list section → explode yields it unchanged → still 1 row, `actualTotals` back-filled from sticky).

- [ ] **Step 9: Full unit suite + lint**

Run: `uv run pytest tests/unit -q`
Expected: PASS.
Run: `uv run ruff check src/ tests/`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add src/component.py tests/unit/test_compute_window.py tests/unit/test_manifest_schema.py
git commit -m "feat: explode timecard_metrics rows + dynamic uniqueId PK + required-group guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Functional tests + recording config

**Files:**
- Modify: `tests/functional/32_timecard_metrics_selected_actual_totals/source/data/config.json`
- Regenerate: `tests/functional/32_timecard_metrics_selected_actual_totals/expected/**`
- Delete: `tests/functional/08_timekeeping_timecard_metrics/` (whole dir)
- Modify: `tests/setup/configs.json` (the timecard_metrics entries)

**Interfaces:** none (test data only).

Rationale for deleting 08: it recorded an **all-sections** response (empty `metric_groups`). That mode is now rejected (a single group is required), and its cassette can't be replayed as a single-section exploded test without re-recording (which needs live credentials). Test 32 (single `ACTUAL_TOTALS`, real `actualTotals` cassette) becomes the canonical functional test; the empty-group failure is covered by the Task 4 unit test.

- [ ] **Step 1: Point test 32 at the single field**

In `tests/functional/32_timecard_metrics_selected_actual_totals/source/data/config.json`, replace:

```json
    "metric_groups": [
      "ACTUAL_TOTALS"
    ],
```

with:

```json
    "metric_group": "ACTUAL_TOTALS",
```

- [ ] **Step 2: Delete the all-sections test 08**

```bash
git rm -r tests/functional/08_timekeeping_timecard_metrics
```

- [ ] **Step 3: Update the recording setup config**

In `tests/setup/configs.json`, find the timecard_metrics entries. For the picker case (currently `"metric_groups": ["ACTUAL_TOTALS"]`, ~line 540), change it to `"metric_group": "ACTUAL_TOTALS"` and update its `description` to say the group is exploded into per-line-item rows. If there is a separate all-sections entry that corresponds to the deleted test 08 (empty/absent `metric_groups`, resource `timekeeping_timecard_metrics`), remove that entry (the scenario no longer exists). Verify the file still parses:

Run: `uv run python -c "import json; json.load(open('tests/setup/configs.json'))"`
Expected: no output.

- [ ] **Step 4: Regenerate test 32's expected output from the EXISTING cassette**

Do **not** re-record (responses are unchanged; only the client-side output shape changed). Delegate to the canonical test tooling — dispatch the `component-developer:tester` agent with:

> "In this repo, regenerate `tests/functional/32_timecard_metrics_selected_actual_totals/expected/` from its EXISTING cassette (`source/data/cassettes/requests.json`). Do NOT re-record — there are no new requests; only the component's client-side output changed (timecard_metrics now explodes the actualTotals section into one row per line item, keyed on `uniqueId`, with applyDate and per-line-item columns). Run `uv run pytest tests/test_functional.py -k 32_timecard_metrics_selected_actual_totals` and update the expected tables + manifest so the test passes. Confirm the new `timekeeping_timecard_metrics.csv` has one row per actualTotals line item (not one per employee), a `uniqueId` column as PK in the manifest, and an `applyDate` column."

If regenerating manually instead: the expected table must have header columns that are the sorted union of the exploded line-item fields (e.g. `applyDate,amountType,daysAmount,employeeId_id,employee_id,hoursAmount,job_id,payCode_id,payPeriodNumber,payPeriodWeek,uniqueId,wages,...` plus any redacted string columns), **one data row per `actualTotals` line item** across all employees, and the manifest must declare `uniqueId` as the (non-nullable) primary key. Employees whose `actualTotals` is empty produce no rows.

- [ ] **Step 5: Run the full functional suite**

Run: `uv run pytest tests/test_functional.py -q`
Expected: PASS (test 08 gone; 32 passes with the exploded expected output; 29 — symbolic/threshold — unaffected as it does not assert metric output shape; if 29 sets `metric_groups`, apply the same single-field rename to its config).

Note: also check `tests/functional/29_timecard_metrics_symbolic_threshold/source/data/config.json` — if it carries `metric_groups`, rename to `metric_group` (single value) and regenerate its expected output the same way if the metric table shape is asserted.

- [ ] **Step 6: Validate cassettes (no secrets/PII, recordings match intent)**

Dispatch the `component-developer:vcr-cassette-validator` agent to validate the timecard_metrics functional cassettes after the changes. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
uv run pytest -q
uv run ruff check src/ tests/
git add tests/functional tests/setup/configs.json
git commit -m "test: single metric_group + exploded expected output; drop all-sections case

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Docs + memory

**Files:**
- Modify: `README.md`
- Modify: `/Users/matyasjirat/.claude/projects/-Users-matyasjirat-VSCodeProjects-Keboola-component-ex-ukg-wfm/memory/ukg-wfm-rollup-vs-per-event-resources.md` (+ `MEMORY.md` index if a new note is added)

**Interfaces:** none.

- [ ] **Step 1: Document the new output shape in the README**

In `README.md`, in the Supported resources / Timekeeping area, add a short note that `timekeeping_timecard_metrics` takes **one** metric group and emits **one row per line item** of that section (with `applyDate` and a `uniqueId` primary key), rather than one JSON blob per employee. Keep it to 1–3 sentences; do not restate the enum. Example wording:

```markdown
> `timekeeping_timecard_metrics` requires a single **Timecard Metric Group**; its response section is
> exploded into one row per line item (columns include `applyDate`, `hoursAmount`, `payCode`, …) with
> `uniqueId` (`employeeId:applyDate:payCode`) as the primary key. Sections that expose `uniqueId`
> upsert incrementally; a section without it falls back to full replace unless you set a Primary Key.
```

- [ ] **Step 2: Update the rollup memory note**

Edit `ukg-wfm-rollup-vs-per-event-resources.md`: keep the rollup/never-date-split fact (the **fetch** is still one window per employee batch), but add a one-line caveat that `timekeeping_timecard_metrics` **output** is now exploded to per-line-item rows keyed on `uniqueId` (client-side; the fetch semantics are unchanged). Do not alter the per-event/rollup lists.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: timecard_metrics single group + exploded per-line-item output

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(The memory file lives outside the repo; it is saved via the memory tooling, not committed.)

---

## Self-Review

**Spec coverage:**
- Single-select `metric_group` + fold + required → Task 2 (field/effective_select/schema), Task 4 (guard). ✓
- Explode transform → Task 1. ✓
- Registry `explode` flag + timecard_metrics config → Task 3. ✓
- Component-determined `uniqueId` PK (dynamic, graceful fallback) → Task 3 (`resolve_primary_key`), Task 4 (both write paths). ✓
- Windowing unchanged → asserted by leaving `window_chunkable`/registry windowing untouched; existing `_NOT_CHUNKABLE` test still covers it. ✓
- Behavior notes (empty section → 0 rows; all-empty → header-only) → Task 1 test + Task 4 test + existing `_write_empty_table`. ✓
- Tests: 32 updated+regenerated, 08 removed, negative case (unit), mock — note: **there is no `tests/mock/` timecard_metrics fixture** (only accruals), so the spec's "mock fixture" item is N/A; recorded in Task 5. ✓
- Docs/README + memory → Task 6. ✓

**Placeholder scan:** No TBD/TODO; every code step has concrete code. Expected-output regeneration in Task 5 is delegated to canonical tooling with an explicit manual fallback (the exact target shape is specified). ✓

**Type consistency:** `explode_record(record) -> Iterator[dict]` (Task 1) is consumed in Task 4's `_rows`. `resolve_primary_key(resource, seen_columns, config_pk=None) -> list[str]` (Task 3) is called with `list(seen_columns)` (Task 4 phase 2) and `sticky` (Task 4 empty path). `metric_group: str | None` (Task 2) is read via `effective_select` (Task 2) and the Task 4 guard. Names consistent across tasks. ✓
