from enum import StrEnum
from typing import Any

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"


class PaginationStyle(StrEnum):
    MULTI_READ = "multi_read"  # employee-batched; whole batch returned in one response
    # apply_read endpoints (persons, punches) page via count+index in the REQUEST BODY: loop
    # incrementing index until a page returns fewer than `count` records. VERIFIED live:
    # persons response is {"totalElements", "records":[...]}; punches is {"metadata", "data":[...]}.
    APPLY_READ = "apply_read"
    NONE = "none"


class IncrementalStyle(StrEnum):
    DATE_WINDOW = "date_window"
    SYMBOLIC_PERIOD = "symbolic_period"
    # NET_CHANGE currently behaves identically to DATE_WINDOW (full refresh). True
    # net-change delta accumulation needs a stable primary key to upsert deltas against
    # plus round-tripping the API's net-change token through state. The net-change
    # resources below have neither, so a real delta would corrupt the table. Enabling
    # true delta is a future registry edit: add a PK + wire token persistence.
    NET_CHANGE = "net_change"
    ASYNC_EXPORT = "async_export"
    NONE = "none"


class EmployeeScope(StrEnum):
    HYPERFIND = "hyperfind"
    # HYPERFIND_SERVER passes the Hyperfind reference straight into the request body so the
    # server does the scoping (Information Access API); no client-side employee-id resolution.
    HYPERFIND_SERVER = "hyperfind_server"
    NONE = "none"


class BodyStyle(StrEnum):
    """How the request body carries employee scope + date range for a given endpoint.

    These were verified against the live UKG Pro WFM tenant — each family rejects the
    others' shapes with a WFP-90011 "Unrecognized property" error.
    """

    # {"where": {"employees": {"ids": [...]}, "dateRange": {"startDate","endDate"}}}
    WHERE_EMPLOYEES_IDS = "where_employees_ids"
    # {"where": {"employees": {"employeeRefs": {"ids": [...]}, "startDate","endDate"}}}
    WHERE_EMPLOYEE_REFS = "where_employee_refs"
    # {"where": {"employees": [{"id":...}, ...], "dateRange": {"startDate","endDate"}}}  (employees is a LIST)
    # VERIFIED live for attestations (POST /timekeeping/attestation/multi_read).
    WHERE_EMPLOYEES_LIST = "where_employees_list"
    # {"employees": {"ids": [...]}, "dateRange": {"startDate","endDate"}}  (no "where" wrapper)
    TOP_EMPLOYEES_IDS = "top_employees_ids"
    # {"where": {"employeeSet": {"employees": {"ids": [...]}, "dateRange": {"startDate","endDate"}}}, "metrics":[...]}
    # VERIFIED live for timekeeping_timecard_metrics (POST /timekeeping/timecard_metrics/multi_read).
    EMPLOYEE_SET_METRICS = "employee_set_metrics"
    # apply_read persons: {"where": {...static filter}} + count/index injected by the paginator.
    # Org-wide bulk read, no employee scope, no date window. VERIFIED live.
    APPLY_READ_PERSONS = "apply_read_persons"
    # apply_read punches: {"where": {"employees": {"ids": [...]},
    #   "dateRange": {"startDateTime","endDateTime"}}} + count/index injected by the paginator.
    # VERIFIED live: where.dateRange takes DATETIME keys and the span must be <= 60 minutes
    # (WTK-124919); count must be 1..25 (WTK-124921).
    APPLY_READ_PUNCHES = "apply_read_punches"
    # net-change: {"where": {"employees": {"ids": [...]},
    #   "lastRunDateTime","endDateTime"}, "select":"SEGMENTS"} — shape VERIFIED (403 WFA-000030,
    #   Activities Integration API not licensed on this tenant), not a 200.
    NET_CHANGE_SEGMENTS = "net_change_segments"
    # {"where": {"employees": {"employees": [{"id":...}], "startDate","endDate"}}} — the swap
    # "employees" criterion nests an ARRAY of employee refs plus its own start/end dates (NOT a
    # {"ids":[...]} object). VERIFIED live 200 for scheduling/employee_swap/multi_read.
    SWAP_EMPLOYEES = "swap_employees"
    # {"where": {"query": {"context":"ORG", "date": <snapshot>, "q": "/"}}} — legacy
    # /commons/locations/multi_read org-map keyword search. VERIFIED live 200 (root-list of org nodes).
    LOCATIONS_QUERY = "locations_query"
    # body is emitted verbatim from body_template (no employee/date injection). Used by org-level
    # resources with a fixed criteria object (e.g. generic_locations).
    STATIC = "static"
    # no body (GET)
    NONE = "none"


# Global fallback sub-window span for a window-chunkable resource when neither the config nor the
# resource sets one. Matches the historical behaviour (one 365-day window for any realistic range).
_DEFAULT_WINDOW_DAYS = 365


class ResourceDef(BaseModel):
    name: str
    family: str
    method: HttpMethod
    endpoint_path: str
    body_template: dict[str, Any] = Field(default_factory=dict)
    body_style: BodyStyle = BodyStyle.WHERE_EMPLOYEES_IDS
    select: list[str] = Field(default_factory=list)
    employee_scope: EmployeeScope = EmployeeScope.NONE
    batch_limit: int = 0
    pagination: PaginationStyle = PaginationStyle.NONE
    # apply_read page size (the count/index body param). persons max 10000; punches 1..25.
    page_count: int = 0
    incremental_style: IncrementalStyle = IncrementalStyle.NONE
    date_field: str | None = None
    # When > 0 the date window is split into sub-windows of at most this many minutes and the
    # bounds are emitted as DATETIME (YYYY-MM-DDTHH:MM:SS). Punches require this (<= 60 min/call).
    window_max_minutes: int = 0
    # Response envelope key holding the record array. None => root-level list (or the legacy
    # ("records","result","data") fallback). A dict value under the key is wrapped as one row.
    records_key: str | None = None
    primary_key: list[str] = Field(default_factory=list)
    # When True, each API record is expanded into one row per line item of its single list section
    # (see transform.explode_record) instead of one row with the list JSON-serialized. Used by
    # timecard_metrics so the chosen metric section becomes per-line-item rows; the PK is then
    # resolved dynamically (resolve_primary_key) rather than from a static registry key.
    explode: bool = False
    # Metric-group (API `select` token) exception to `window_chunkable`, for EMPLOYEE_SET_METRICS.
    # The resource-level rule fails closed because the endpoint returns ONE period-rollup row per
    # employee for most selects. A group listed here is different: its records are EXPLODED into
    # per-line-item rows that each carry their own `applyDate`, so the rows of [A,B] + [B+1,C] are
    # exactly the rows of [A,C] — splitting the window partitions the rows instead of aggregating
    # them, and is therefore safe. Only groups VERIFIED to be per-line-item belong here
    # (CFTL-814: ACTUAL_TOTALS — a production 3-week read returned 3,045,591 rows for ~16k
    # employees, one per line item, each with applyDate + uniqueId). Every other group stays
    # fail-closed until a live read proves the same shape; add it here once verified.
    chunkable_select: list[str] = Field(default_factory=list)
    # Default sub-window span (days) used when the resource is window-chunkable and the config
    # leaves Window Chunk Size empty. 0 = use the global 365-day default. A small value is the
    # point for a high-volume exploded read: a multi-month pull of millions of per-line-item rows
    # is what hits the 256 MB limit, and only date-splitting reduces the per-response size for a
    # resource whose rows are already spread over the whole range.
    default_window_days: int = 0
    # A static schema FLOOR for this resource's output, keyed by the lowercased metric-group /
    # output-name suffix (the same suffix Component._output_name() appends, e.g. "actual_totals").
    # These are columns KNOWN to exist for that output table — always emitted (empty when a given
    # run's data omits them) regardless of which config row runs or whether that row has any sticky
    # state yet.
    #
    # ROOT CAUSE (CFTL-814): the output table is derived from resource.name + metric group ONLY, not
    # the config row id, so several config rows (e.g. different Hyperfinds or date ranges) can
    # legitimately write into ONE shared Storage table. The column set that guarantees a run's CSV
    # never has fewer columns than the destination lives in state.json, which Keboola scopes PER
    # CONFIG ROW — a newly created row starts with EMPTY state. If that new row's data happens to
    # omit an optional field the shared table already has (observed in production: `payPeriodWeek`
    # missing from a `timekeeping_timecard_metrics_actual_totals` run), the load fails with "Some
    # columns are missing in the csv file". Making the floor a property of the RESOURCE (this field)
    # rather than of one row's run history fixes that: it is unioned in alongside the sticky state
    # (see Component._known_columns_floor / _extend_sticky_columns / _write_empty_table) so the
    # emitted schema is deterministic from the first run of any row. Empty (the default) = no change
    # for every other resource.
    known_columns: dict[str, list[str]] = Field(default_factory=dict)

    def known_columns_floor(self, suffix: str | None) -> list[str]:
        """The known-columns schema floor for this resource's `suffix`-keyed output (see above)."""
        return self.known_columns.get(suffix, []) if suffix else []

    @property
    def window_chunkable(self) -> bool:
        """True when splitting the fetch window into date sub-windows yields correct rows.

        Only per-event, employee-scoped, calendar-date resources qualify — the ones whose rows are
        individual timestamped events (shifts, timecards, leave/attendance records, attestations), so
        partitioning by date just partitions the rows. Excluded, fail-closed:
        - EMPLOYEE_SET_METRICS (timecard_metrics + accruals) return ONE period-rollup row per
          employee — VERIFIED live: a [2026-04-21, 2026-07-27] read returns one row per employee with
          the daily detail nested — so splitting would emit a partial-period row per sub-window
          (collapsing under the employeeId_id upsert, or inflating a full load).
        - Punch-style resources have their own per-call minute cap (window_max_minutes > 0).
        - Net-change resources aren't a plain date window (IncrementalStyle.NET_CHANGE).
        - Org-level resources (employee_scope != HYPERFIND, e.g. forecasting) whose row/aggregation
          semantics aren't verified — excluded until proven splittable.
        For the excluded ones the memory lever is `batch_size` (employee chunking), not `window_days`.
        """
        return (
            self.incremental_style == IncrementalStyle.DATE_WINDOW
            and self.body_style != BodyStyle.EMPLOYEE_SET_METRICS
            and self.window_max_minutes == 0
            and self.employee_scope == EmployeeScope.HYPERFIND
        )

    def is_window_chunkable(self, select: list[str] | None = None) -> bool:
        """True when this run's fetch window may be split into date sub-windows.

        Same question as `window_chunkable`, but answered for the ACTUAL run rather than for the
        resource alone: an EMPLOYEE_SET_METRICS resource is excluded at resource level because most
        metric groups return a period rollup, yet a group listed in `chunkable_select` explodes into
        per-line-item rows and IS splittable (see `chunkable_select`). Exactly one select token must
        be chosen and it must be in that list — a multi-group read has no verified row shape, so it
        stays fail-closed.
        """
        if self.window_chunkable:
            return True
        if not self.chunkable_select or self.incremental_style != IncrementalStyle.DATE_WINDOW:
            return False
        if self.window_max_minutes > 0 or self.employee_scope != EmployeeScope.HYPERFIND:
            return False
        return bool(select) and len(select) == 1 and select[0] in self.chunkable_select

    def window_days_for(self, select: list[str] | None = None, window_days: int | None = None) -> int:
        """Sub-window span (days) for this run: the config value, else the per-resource default."""
        if window_days:
            return window_days
        if self.default_window_days and self.is_window_chunkable(select):
            return self.default_window_days
        return _DEFAULT_WINDOW_DAYS


RESOURCE_REGISTRY: dict[str, ResourceDef] = {
    # VERIFIED live: POST /commons/persons/apply_read is a bulk (org-wide) read that pages via
    # count/index in the request body. Response envelope {"totalElements","records":[...]}.
    "persons": ResourceDef(
        name="persons",
        family="people",
        method=HttpMethod.POST,
        endpoint_path="/commons/persons/apply_read",
        body_style=BodyStyle.APPLY_READ_PERSONS,
        body_template={"where": {}},
        employee_scope=EmployeeScope.NONE,
        batch_limit=0,
        page_count=1000,
        pagination=PaginationStyle.APPLY_READ,
        incremental_style=IncrementalStyle.NONE,
        records_key="records",
        primary_key=["personNumber"],
    ),
    # VERIFIED live: /commons/generic_locations/multi_read is 403 here ("Simplified Business Structure"
    # feature off), so we use the legacy /commons/locations/multi_read, which returns 200 on this
    # tenant. It is an org-map keyword search: where.query needs context ("ORG"), a snapshot date
    # (defaulted to the run's upper bound), and a query string q ("/"). Response is a root-level list
    # of org-map nodes keyed by nodeId. NOTE: q="/" returns the top/matching nodes; a full recursive
    # descendantsOf traversal of the whole hierarchy is future work (see report).
    "business_structure": ResourceDef(
        name="business_structure",
        family="business_structure",
        method=HttpMethod.POST,
        endpoint_path="/commons/locations/multi_read",
        body_style=BodyStyle.LOCATIONS_QUERY,
        employee_scope=EmployeeScope.NONE,
        batch_limit=0,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.NONE,
        records_key=None,
        primary_key=["nodeId"],
    ),
    # VERIFIED against live tenant: GET returns {"hyperfindQueries": [{id,name,...}]}.
    "hyperfind_queries": ResourceDef(
        name="hyperfind_queries",
        family="hyperfind",
        method=HttpMethod.GET,
        endpoint_path="/commons/hyperfind",
        body_style=BodyStyle.NONE,
        employee_scope=EmployeeScope.NONE,
        batch_limit=0,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.NONE,
        records_key="hyperfindQueries",
        primary_key=["id"],
    ),
    # VERIFIED live: POST /timekeeping/punches/apply_read. where.employees.ids + where.dateRange with
    # DATETIME keys, count/index body paging. Response envelope {"metadata","data":[...]}; each data
    # item is one employee with {employee, punches[]}. HARD API CONSTRAINTS: the dateRange span must
    # be <= 60 minutes (WTK-124919) and count 1..25 (WTK-124921) — so this behaves as a near-real-time
    # incremental reader; a large backfill fans out into one call per 60-minute window per employee
    # batch. No cross-window-stable PK (punches are embedded per employee/window) -> full REPLACE.
    "timekeeping_punches": ResourceDef(
        name="timekeeping_punches",
        family="timekeeping",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/punches/apply_read",
        body_style=BodyStyle.APPLY_READ_PUNCHES,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=100,
        page_count=25,
        pagination=PaginationStyle.APPLY_READ,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        window_max_minutes=60,
        records_key="data",
        primary_key=[],
    ),
    # VERIFIED: POST returns a root-level list; each item has employee{id,name}, startDate, punches, etc.
    "timekeeping_timecards": ResourceDef(
        name="timekeeping_timecards",
        family="timekeeping",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/timecard/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEES_IDS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=["employee_id", "startDate"],
    ),
    # VERIFIED live: POST /timekeeping/timecard_metrics/multi_read. Scope nests under
    # where.employeeSet {dateRange, employees{ids}}; the selector is a top-level "select" array (NOT
    # "metrics" — that key is silently ignored). With select empty the API returns ALL rollups.
    # Returns a root-level list, one entry per employee (musterReport*, scheduledTotals,
    # accrualSummaryData, exceptioncounts, ...); employeeId is an object -> flattened employeeId_id.
    "timekeeping_timecard_metrics": ResourceDef(
        name="timekeeping_timecard_metrics",
        family="timekeeping",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/timecard_metrics/multi_read",
        body_style=BodyStyle.EMPLOYEE_SET_METRICS,
        employee_scope=EmployeeScope.HYPERFIND,
        # No `select` -> the API returns ALL rollup sections (~110 KB/employee measured live), so the
        # whole batch response is parsed into memory at once. 500 employees (~55 MB wire, several x
        # that parsed) exceeds the 256 MB component limit; 100 keeps peak well under it. Override per
        # config with `batch_size` if needed. Accruals ride the same endpoint but with a narrow select
        # (much smaller payload), so they keep the larger default.
        batch_limit=100,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        explode=True,
        # Exploded to per-line-item rows; on incremental load the PK is uniqueId when present
        # (resolve_primary_key), so the registry key is empty (dynamic). accruals keep
        # employeeId_id — they are not exploded.
        primary_key=[],
        # CFTL-814 item 3: ACTUAL_TOTALS explodes into per-line-item rows, so its window MAY be
        # date-split (see ResourceDef.chunkable_select). 21 days by default: a multi-month pull of
        # this group is the read that hit the 256 MB limit (3.0M rows / 3 weeks in production), and
        # batch_size alone cannot bound it because every employee has rows in every sub-window.
        chunkable_select=["ACTUAL_TOTALS"],
        default_window_days=21,
        # Known-columns schema floor (see ResourceDef.known_columns) for the ACTUAL_TOTALS metric
        # group's output table. VERIFIED: this is the column set from a successful production run's
        # stored Storage schema for timekeeping_timecard_metrics_actual_totals — always emit these,
        # empty when a given run's data omits one (e.g. `payPeriodWeek`, an optional field not every
        # entry carries), so a brand-new config row sharing that table never narrows its schema.
        known_columns={
            "actual_totals": [
                "amountType",
                "applyDate",
                "combined",
                "daysAmount",
                "employeeId_id",
                "employeeId_name",
                "employeeId_qualifier",
                "employee_id",
                "employee_name",
                "employee_qualifier",
                "hoursAmount",
                "isFromCorrection",
                "jobTransfer",
                "job_id",
                "job_name",
                "job_qualifier",
                "laborCategories_entries",
                "laborCategories_laborString",
                "laborCategories_referenceId",
                "laborTransfer",
                "payCode_id",
                "payCode_name",
                "payCode_qualifier",
                "payPeriodNumber",
                "payPeriodWeek",
                "position_id",
                "position_name",
                "position_qualifier",
                "signedOff",
                "uniqueId",
                "wageAddition",
                "wageMultiplier",
                "wages",
                "wagesCurrency_amount",
                "wagesCurrency_currencyCode",
            ]
        },
    ),
    # VERIFIED: /scheduling/schedule/multi_read returns a COMPOSITE of entity lists (shifts,
    # scheduleDayList, openShifts, holidays, ...). Three registry resources share this one
    # endpoint, each surfacing a different envelope via records_key.
    "scheduling_schedules": ResourceDef(
        name="scheduling_schedules",
        family="scheduling",
        method=HttpMethod.POST,
        endpoint_path="/scheduling/schedule/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEE_REFS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key="scheduleDayList",
        primary_key=["employee_id", "day"],
    ),
    "scheduling_shifts": ResourceDef(
        name="scheduling_shifts",
        family="scheduling",
        method=HttpMethod.POST,
        endpoint_path="/scheduling/schedule/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEE_REFS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key="shifts",
        primary_key=[],
    ),
    "scheduling_open_shifts": ResourceDef(
        name="scheduling_open_shifts",
        family="scheduling",
        method=HttpMethod.POST,
        endpoint_path="/scheduling/schedule/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEE_REFS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key="openShifts",
        primary_key=[],
    ),
    # VERIFIED live 200: POST /scheduling/employee_swap/multi_read. where.employees is a criterion
    # object whose "employees" is an ARRAY of refs [{id}] with sibling startDate/endDate (see
    # BodyStyle.SWAP_EMPLOYEES) — a bare {"ids":[...]} is rejected WFP-90009. Root-level list of
    # swap requests. No row observed in the probe window (0 swaps), so PK stays empty (full REPLACE);
    # the authoritative spec's stable key is the request `id` if a PK is wanted once rows are seen.
    "scheduling_swaps": ResourceDef(
        name="scheduling_swaps",
        family="scheduling",
        method=HttpMethod.POST,
        endpoint_path="/scheduling/employee_swap/multi_read",
        body_style=BodyStyle.SWAP_EMPLOYEES,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=[],
    ),
    # VERIFIED live 200: the three accruals resources all ride POST /timekeeping/timecard_metrics/
    # multi_read (there is no bulk /accruals/*/multi_read). They differ only by the `select` value.
    # Response is a root-level list, one entry per employee; employeeId is an object -> employeeId_id
    # is the stable PK. ACCRUAL_SUMMARY carries balances (accrualSummaryData[].dailySummaries[]
    # .currentBalance/availableBalance*), so balances + summaries share that select; transactions
    # uses ACCRUAL_TRANSACTIONS (accrualTransactions[]).
    "accruals_balances": ResourceDef(
        name="accruals_balances",
        family="accruals",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/timecard_metrics/multi_read",
        body_style=BodyStyle.EMPLOYEE_SET_METRICS,
        select=["ACCRUAL_SUMMARY"],
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=["employeeId_id"],
    ),
    "accruals_transactions": ResourceDef(
        name="accruals_transactions",
        family="accruals",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/timecard_metrics/multi_read",
        body_style=BodyStyle.EMPLOYEE_SET_METRICS,
        select=["ACCRUAL_TRANSACTIONS"],
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=["employeeId_id"],
    ),
    "accruals_summaries": ResourceDef(
        name="accruals_summaries",
        family="accruals",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/timecard_metrics/multi_read",
        body_style=BodyStyle.EMPLOYEE_SET_METRICS,
        select=["ACCRUAL_SUMMARY"],
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=["employeeId_id"],
    ),
    # VERIFIED: POST /leave/leave_cases/multi_read with top-level {"employees":{"ids"},"dateRange"}
    # (no "where" wrapper). Returns a root-level list.
    "leave_cases": ResourceDef(
        name="leave_cases",
        family="leave",
        method=HttpMethod.POST,
        endpoint_path="/leave/leave_cases/multi_read",
        body_style=BodyStyle.TOP_EMPLOYEES_IDS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=[],
    ),
    # VERIFIED live: POST /leave/leave_edits/multi_read with a top-level {dateRange, employees{ids}}
    # (no "where" wrapper, no search-type enum). Response is a dict; the record array is under
    # "leaveEdits" (alongside totals/leaveCaseTotals/metadata).
    "leave_edits": ResourceDef(
        name="leave_edits",
        family="leave",
        method=HttpMethod.POST,
        endpoint_path="/leave/leave_edits/multi_read",
        body_style=BodyStyle.TOP_EMPLOYEES_IDS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key="leaveEdits",
        primary_key=[],
    ),
    # VERIFIED live: POST /attendance/actions/multi_read with a top-level {employees{ids}, dateRange}
    # (no "where" wrapper). Returns a root-level list.
    "attendance_records": ResourceDef(
        name="attendance_records",
        family="attendance",
        method=HttpMethod.POST,
        endpoint_path="/attendance/actions/multi_read",
        body_style=BodyStyle.TOP_EMPLOYEES_IDS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=[],
    ),
    # VERIFIED: POST /attendance/events/multi_read with top-level {"employees":{"ids"},"dateRange"}
    # (no "where" wrapper). Returns a root-level list.
    "attendance_events": ResourceDef(
        name="attendance_events",
        family="attendance",
        method=HttpMethod.POST,
        endpoint_path="/attendance/events/multi_read",
        body_style=BodyStyle.TOP_EMPLOYEES_IDS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=[],
    ),
    # VERIFIED live: POST /timekeeping/attestation/multi_read. where.employees is a LIST of {id}
    # refs + where.dateRange, and a top-level "select" (array). Returns a root-level list; each item
    # holds attestationDailyDetail. No stable PK exposed -> full REPLACE.
    "attestations": ResourceDef(
        name="attestations",
        family="attestations",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/attestation/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEES_LIST,
        body_template={"select": ["attestationDailyDetail"]},
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=[],
    ),
    # Shape RESOLVED against the authoritative spec + live: POST /work/employee_activities/multi_read
    # takes where.employees.ids and has NO dateRange (it returns the activities *assigned to* each
    # employee, not a time-ranged transaction list) — hence date_field=None. On this tenant it 403s
    # with WFA-000030 ("does not have access to Activities Integration API"), same licensing block as
    # the other work/* resources, so a 200 could not be observed here. No stable PK -> full REPLACE.
    "work_activities": ResourceDef(
        name="work_activities",
        family="work",
        method=HttpMethod.POST,
        endpoint_path="/work/employee_activities/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEES_IDS,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.NONE,
        date_field=None,
        records_key=None,
        primary_key=[],
    ),
    # Shape VERIFIED (403 WFA-000030: Activities Integration API not licensed on this tenant, so no
    # 200): where.employees.ids + where.dateRange{startDate,endDate} + top-level select "SEGMENTS".
    "work_activity_shifts": ResourceDef(
        name="work_activity_shifts",
        family="work",
        method=HttpMethod.POST,
        endpoint_path="/work/activity_shifts/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEES_IDS,
        body_template={"select": "SEGMENTS"},
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.MULTI_READ,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        primary_key=[],
    ),
    # Authoritative path /work/activity_shifts/net_changes/multi_read. Shape VERIFIED (403
    # WFA-000030: Activities Integration API not licensed here, so no 200): where.employees.ids +
    # where.lastRunDateTime + where.endDateTime (DATETIME) + top-level select "SEGMENTS". No stable
    # PK -> case-2 full refresh (date window + REPLACE), NOT a true net-change delta. See
    # IncrementalStyle.NET_CHANGE for what enabling real delta needs.
    "work_activity_net_changes": ResourceDef(
        name="work_activity_net_changes",
        family="work",
        method=HttpMethod.POST,
        endpoint_path="/work/activity_shifts/net_changes/multi_read",
        body_style=BodyStyle.NET_CHANGE_SEGMENTS,
        body_template={"select": "SEGMENTS"},
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=50,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.NET_CHANGE,
        date_field="start",
        primary_key=[],
    ),
    # VERIFIED live: async export. Submit POST /commons/payroll/export/async returns 202 with
    # {"executionKey","state",...}; poll GET /commons/payroll/export/async for the matching
    # executionKey until state=SUCCESSFUL; fetch GET /commons/payroll/export/async/{key}/response
    # (CSV). The submit body requires a tenant-defined SQL-like `query` (config `payroll_query`).
    "payroll_export": ResourceDef(
        name="payroll_export",
        family="payroll",
        method=HttpMethod.POST,
        endpoint_path="/commons/payroll/export/async",
        employee_scope=EmployeeScope.NONE,
        batch_limit=0,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.ASYNC_EXPORT,
        date_field=None,
        primary_key=[],
    ),
    # Path /forecasting/volume_forecasts/multi_read confirmed. Authoritative where shape:
    #   {"where": {"categoryDrivers": [{"category": {"id": <n>}, "drivers": {"ids": [...]}}],
    #              "dateRange": {"startDate","endDate"}}}
    # categoryDrivers must be non-empty (else WFF-270000) and its category/driver refs are
    # TENANT-SPECIFIC (discover via GET /forecasting/category_profiles + GET /forecasting/
    # volume_drivers). TODO: this tenant returns EMPTY lists for both volume_drivers and
    # labor_standards — i.e. NO forecast categories/drivers are configured — so categoryDrivers
    # cannot be populated and a 200 is unreachable here (WFF-270000). Wiring this up needs a config
    # field carrying the tenant's category/driver refs (or a discovery pre-step); left as a TODO.
    "forecasting": ResourceDef(
        name="forecasting",
        family="forecasting",
        method=HttpMethod.POST,
        endpoint_path="/forecasting/volume_forecasts/multi_read",
        body_style=BodyStyle.WHERE_EMPLOYEES_IDS,
        employee_scope=EmployeeScope.NONE,
        batch_limit=0,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        primary_key=[],
    ),
}


def get_resource(name: str) -> ResourceDef:
    try:
        return RESOURCE_REGISTRY[name]
    except KeyError as e:
        raise UserException(f"Unknown resource '{name}'. Valid resources: {', '.join(RESOURCE_REGISTRY)}") from e


def effective_primary_key(resource: ResourceDef, config_pk: list[str] | None = None) -> list[str]:
    """The primary key for the output table: the user-supplied one wins over the registry default."""
    return config_pk or resource.primary_key


# The natural per-line-item key exploded metric rows expose (employeeId:applyDate:payCode).
_EXPLODE_PK = "uniqueId"


def resolve_primary_key(
    resource: ResourceDef,
    seen_columns: list[str],
    config_pk: list[str] | None = None,
    incremental: bool = True,
) -> list[str]:
    """The output-table primary key, resolving the dynamic key for exploded resources.

    Precedence for an exploded resource:

    1. An explicit user `primary_key` wins. It is the escape hatch for a tenant whose line items
       need a wider key (e.g. uniqueId + job_id + labor account) and it is never silently replaced.
    2. Otherwise, on INCREMENTAL load, `uniqueId` (the per-line-item key, `employeeId:applyDate:
       payCode`) is used when the exploded rows expose it — an upsert has to have some key, and
       without one the write would append duplicates unboundedly. The caller warns that this key
       can collapse line items (see below).
    3. Otherwise (FULL load), NO key. A full load does not need one, and `uniqueId` is not unique.

    CFTL-814 (item 4) — why full load is now keyless. `uniqueId` is `employeeId:applyDate:payCode`,
    which repeats when one employee works several jobs on one day. Storage then keeps ONE row per
    key on import and drops the rest, so hours went missing with no error. The collision was not
    reproducible from stored data precisely BECAUSE the key hid it: the stored table is unique by
    construction. The customer proved it against a keyless copy of the same read (3 weeks,
    724,379 rows): 2,401 uniqueId values carried more than one line and 2,461 lines were dropped in
    production — e.g. one employee on 2026-09-12 with REG on two jobs kept 2.43 h and lost 8.98 h.
    No candidate key is unique either (job + labor account still collapsed 19 rows; rows repeat
    legitimately, e.g. six separate 1-hour rest premiums on one day), so the correct answer for a
    full load is no key at all rather than a wider one.

    MIGRATION: none needed. Keboola's output mapping compares the manifest key with an EXISTING
    table's key and changes it on import (removeTablePrimaryKey, then createTablePrimaryKey for a
    non-empty key), so a table keyed on uniqueId loses that key on the first keyless full load. A
    NEW key can only be created when the stored rows are unique on it — e.g. going back from a
    keyless table to an incremental uniqueId upsert fails until the table is emptied. The component
    logs which key it chose (see Component._warn_exploded_key).

    Incremental load keeps `uniqueId` because an upsert needs a key; it remains lossy for
    multi-job days, which is why full load is the documented choice for this resource.
    """
    if resource.explode and not config_pk and incremental and _EXPLODE_PK in seen_columns:
        return [_EXPLODE_PK]
    return effective_primary_key(resource, config_pk)


def effective_incremental(resource: ResourceDef, incremental_load: bool, config_pk: list[str] | None = None) -> bool:
    """Whether a run writes to Storage *incrementally*, given a resource and a static config PK.

    A run upserts (append + PK dedup, manifest `incremental=True`) ONLY when the user selected
    incremental_load AND there is a stable primary key to upsert against — either the resource
    registry default OR a user-supplied `primary_key`. Without any PK, an incremental append would
    duplicate rows unboundedly, so such a resource must run as a full REPLACE every run regardless
    of the configured load type.

    The component's write path (`component.py`) no longer calls this function directly: it inlines
    the equivalent `incremental_load and bool(primary_key)` using the *dynamically resolved* PK from
    `resolve_primary_key` (which accounts for `seen_columns`), so exploded resources whose PK is the
    runtime-discovered `uniqueId` are handled correctly — something a static `config_pk` alone can't
    express. This function remains the documented reference for the incremental rule on
    keyless-vs-keyed resources and is covered by `tests/unit/test_incremental_model.py`.
    """
    return incremental_load and bool(effective_primary_key(resource, config_pk))
