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
    # {"select": [{"key":...}], "from": {"view":"EMP","employeeSet":{"hyperfind":{"id":ref},"dateRange":{...}}}}
    INFO_ACCESS = "info_access"
    # body is emitted verbatim from body_template (no employee/date injection). Used by org-level
    # resources with a fixed criteria object (e.g. generic_locations).
    STATIC = "static"
    # no body (GET)
    NONE = "none"


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


def _date_window(name: str, family: str, endpoint_path: str) -> ResourceDef:
    """Standard hyperfind-scoped date-windowed multi_read resource (≤500 emp/call).

    NOTE: these paths were NOT confirmed against the live tenant (they 404 there). They are
    retained for config back-compat pending authoritative path confirmation; the verified
    resources below override the ones we could ground.
    """
    return ResourceDef(
        name=name,
        family=family,
        method=HttpMethod.POST,
        endpoint_path=endpoint_path,
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.MULTI_READ,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        primary_key=[],
    )


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
    # VERIFIED live: /commons/business_structure/multi_read is 404 on this tenant. The authoritative
    # path is /commons/generic_locations/multi_read, which requires the "Simplified Business
    # Structure" feature switch (403 WCO-103232 here). The where accepts a locations criteria object;
    # emitted verbatim. Response fields could not be confirmed (feature disabled) -> no PK (REPLACE).
    "business_structure": ResourceDef(
        name="business_structure",
        family="business_structure",
        method=HttpMethod.POST,
        endpoint_path="/commons/generic_locations/multi_read",
        body_style=BodyStyle.STATIC,
        body_template={"where": {"locations": {"qualifiers": ["/"]}}},
        employee_scope=EmployeeScope.NONE,
        batch_limit=0,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.NONE,
        primary_key=[],
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
    # VERIFIED path/body; returns {"metadata":{...,"cacheKey"}, "data":{...}} — a hierarchical
    # node, NOT a tabular list. Scoped server-side via from.employeeSet.hyperfind.id (needs a
    # hyperfind_ref). records_key="data" yields the single tree node as one flattened row; a
    # proper tabular unnest of data.children is future work (see report).
    "information_access": ResourceDef(
        name="information_access",
        family="information_access",
        method=HttpMethod.POST,
        endpoint_path="/commons/data/multi_read",
        body_style=BodyStyle.INFO_ACCESS,
        employee_scope=EmployeeScope.HYPERFIND_SERVER,
        batch_limit=0,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key="data",
        primary_key=[],
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
    # where.employeeSet {dateRange, employees{ids}}; a top-level "metrics" array selects the rollups.
    # Returns a root-level list, one entry per employee (musterReport*, scheduledTotals,
    # accrualSummaryData, exceptioncounts, ...). PK = employeeId (one rolled-up row per employee).
    "timekeeping_timecard_metrics": ResourceDef(
        name="timekeeping_timecard_metrics",
        family="timekeeping",
        method=HttpMethod.POST,
        endpoint_path="/timekeeping/timecard_metrics/multi_read",
        body_style=BodyStyle.EMPLOYEE_SET_METRICS,
        body_template={"metrics": ["ACTUAL_HOURS"]},
        employee_scope=EmployeeScope.HYPERFIND,
        batch_limit=500,
        pagination=PaginationStyle.NONE,
        incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start",
        records_key=None,
        primary_key=["employeeId"],
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
    # Authoritative path /scheduling/employee_swap/multi_read (old /scheduling/request_swaps 404s).
    # UNRESOLVED body shape: every probed where.employees form (ids / employeeRefs / [{id}] /
    # int-list / employeeSet / qualifiers) returns WFP-90009 "incorrect data type". Path is fixed
    # but the request will 400 until the correct where schema for employee_swap is confirmed. TODO.
    "scheduling_swaps": _date_window("scheduling_swaps", "scheduling", "/scheduling/employee_swap/multi_read"),
    "accruals_balances": _date_window("accruals_balances", "accruals", "/accruals/balances/multi_read"),
    "accruals_transactions": _date_window("accruals_transactions", "accruals", "/accruals/transactions/multi_read"),
    "accruals_summaries": _date_window("accruals_summaries", "accruals", "/accruals/summary/multi_read"),
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
    "leave_requests": _date_window("leave_requests", "leave", "/leave/requests/multi_read"),
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
    "attendance_patterns": _date_window("attendance_patterns", "attendance", "/attendance/patterns/multi_read"),
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
    # Authoritative path /work/employee_activities/multi_read (old /activities/multi_read 404s).
    # UNRESOLVED body shape: with where.employees as {ids} the dateRange is rejected (WFP-90011);
    # with employees as a list the request returns WFP-90009 "incorrect data type" for every date
    # shape tried (dateRange{startDate,endDate} / {startDateTime,endDateTime} / employeeSet). Path
    # is fixed but requests will 400 until the correct where schema is confirmed. TODO.
    "work_activities": _date_window("work_activities", "work", "/work/employee_activities/multi_read"),
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
    # Authoritative path /forecasting/volume_forecasts/multi_read (old /forecasting/volume 404s).
    # UNRESOLVED: the where shape needs a non-empty `categoryDrivers` collection (WFF-270000) whose
    # values are tenant-specific forecast driver refs not available to this probe. Path + where.
    # dateRange are correct; the request 400s until categoryDrivers are supplied. TODO.
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


def effective_incremental(resource: ResourceDef, incremental_load: bool) -> bool:
    """Single source of truth for whether a run is *effectively* incremental.

    A run behaves incrementally (append/upsert with PK dedup, a state watermark, and
    a [last_run - overlap, now] window) ONLY when the user selected incremental_load
    AND the resource has a stable primary key to upsert against.

    Without a PK, an incremental append would duplicate rows unboundedly, so such a
    resource must run as a full REPLACE every run (case-2 full refresh) regardless of
    the configured load type. This one predicate governs all three of: reading/advancing
    the state watermark, computing the fetch window, and the manifest `incremental` flag.
    They must never disagree.
    """
    return incremental_load and bool(resource.primary_key)
