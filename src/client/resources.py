from enum import StrEnum

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"


class PaginationStyle(StrEnum):
    MULTI_READ = "multi_read"  # cacheKey + count + index
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
    NONE = "none"


class ResourceDef(BaseModel):
    name: str
    family: str
    method: HttpMethod
    endpoint_path: str
    body_template: dict = Field(default_factory=dict)
    select: list[str] = Field(default_factory=list)
    employee_scope: EmployeeScope = EmployeeScope.NONE
    batch_limit: int = 0
    pagination: PaginationStyle = PaginationStyle.NONE
    incremental_style: IncrementalStyle = IncrementalStyle.NONE
    date_field: str | None = None
    primary_key: list[str] = Field(default_factory=list)
    confirmed: bool = True


def _date_window(name: str, family: str, endpoint_path: str) -> ResourceDef:
    """Standard hyperfind-scoped date-windowed multi_read resource (≤500 emp/call)."""
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
    "persons": ResourceDef(
        name="persons", family="people", method=HttpMethod.POST,
        endpoint_path="/commons/persons/multi_read",
        employee_scope=EmployeeScope.HYPERFIND, batch_limit=100,
        pagination=PaginationStyle.MULTI_READ, incremental_style=IncrementalStyle.NONE,
        primary_key=["personIdentity_personNumber"],
    ),
    "business_structure": ResourceDef(
        name="business_structure", family="business_structure", method=HttpMethod.POST,
        endpoint_path="/commons/business_structure/multi_read",
        employee_scope=EmployeeScope.NONE, batch_limit=5000,
        pagination=PaginationStyle.MULTI_READ, incremental_style=IncrementalStyle.NONE,
        primary_key=["id"],
    ),
    "hyperfind_queries": ResourceDef(
        name="hyperfind_queries", family="hyperfind", method=HttpMethod.GET,
        endpoint_path="/commons/hyperfind",
        employee_scope=EmployeeScope.NONE, batch_limit=0,
        pagination=PaginationStyle.NONE, incremental_style=IncrementalStyle.NONE,
        primary_key=["id"],
    ),
    "information_access": _date_window(
        "information_access", "information_access", "/commons/data/multi_read"
    ),
    "timekeeping_punches": _date_window(
        "timekeeping_punches", "timekeeping", "/timekeeping/punches/multi_read"
    ),
    "timekeeping_timecards": _date_window(
        "timekeeping_timecards", "timekeeping", "/timekeeping/timecard/multi_read"
    ),
    "timekeeping_timecard_metrics": _date_window(
        "timekeeping_timecard_metrics", "timekeeping", "/timekeeping/timecard_metrics/multi_read"
    ),
    "scheduling_schedules": _date_window(
        "scheduling_schedules", "scheduling", "/scheduling/schedule/multi_read"
    ),
    "scheduling_shifts": _date_window(
        "scheduling_shifts", "scheduling", "/scheduling/shifts/multi_read"
    ),
    "scheduling_open_shifts": _date_window(
        "scheduling_open_shifts", "scheduling", "/scheduling/open_shifts/multi_read"
    ),
    "scheduling_swaps": _date_window(
        "scheduling_swaps", "scheduling", "/scheduling/request_swaps/multi_read"
    ),
    "accruals_balances": _date_window(
        "accruals_balances", "accruals", "/accruals/balances/multi_read"
    ),
    "accruals_transactions": _date_window(
        "accruals_transactions", "accruals", "/accruals/transactions/multi_read"
    ),
    "accruals_summaries": _date_window(
        "accruals_summaries", "accruals", "/accruals/summary/multi_read"
    ),
    "leave_cases": _date_window("leave_cases", "leave", "/leave/cases/multi_read"),
    "leave_edits": _date_window("leave_edits", "leave", "/leave/edits/multi_read"),
    "leave_requests": _date_window("leave_requests", "leave", "/leave/requests/multi_read"),
    "attendance_records": _date_window(
        "attendance_records", "attendance", "/attendance/records/multi_read"
    ),
    "attendance_patterns": _date_window(
        "attendance_patterns", "attendance", "/attendance/patterns/multi_read"
    ),
    "attendance_events": _date_window(
        "attendance_events", "attendance", "/attendance/events/multi_read"
    ),
    "attestations": _date_window(
        "attestations", "attestations", "/attestation/process_profiles/multi_read"
    ),
    "work_activities": _date_window("work_activities", "work", "/activities/multi_read"),
    # No stable PK -> runs as a case-2 full refresh (date window + REPLACE), NOT a true
    # net-change delta. See IncrementalStyle.NET_CHANGE for what enabling real delta needs.
    "work_activity_net_changes": ResourceDef(
        name="work_activity_net_changes", family="work", method=HttpMethod.POST,
        endpoint_path="/activities/net_changes/multi_read",
        employee_scope=EmployeeScope.HYPERFIND, batch_limit=50,
        pagination=PaginationStyle.MULTI_READ, incremental_style=IncrementalStyle.NET_CHANGE,
        date_field="start", primary_key=[],
    ),
    "payroll_export": ResourceDef(
        name="payroll_export", family="payroll", method=HttpMethod.POST,
        endpoint_path="/payroll/export",
        employee_scope=EmployeeScope.HYPERFIND, batch_limit=500,
        pagination=PaginationStyle.NONE, incremental_style=IncrementalStyle.ASYNC_EXPORT,
        date_field="applyDate", primary_key=[],
    ),
    "forecasting": ResourceDef(
        name="forecasting", family="forecasting", method=HttpMethod.POST,
        endpoint_path="/forecasting/volume/multi_read",
        employee_scope=EmployeeScope.NONE, batch_limit=0,
        pagination=PaginationStyle.MULTI_READ, incremental_style=IncrementalStyle.DATE_WINDOW,
        date_field="start", primary_key=[],
    ),
}


def get_resource(name: str) -> ResourceDef:
    try:
        return RESOURCE_REGISTRY[name]
    except KeyError as e:
        raise UserException(
            f"Unknown resource '{name}'. Valid resources: {', '.join(RESOURCE_REGISTRY)}"
        ) from e


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
