import pytest
from keboola.component.exceptions import UserException

from client.resources import (
    RESOURCE_REGISTRY,
    BodyStyle,
    EmployeeScope,
    IncrementalStyle,
    PaginationStyle,
    get_resource,
)

EXPECTED_FAMILIES = {
    "people",
    "business_structure",
    "hyperfind",
    "information_access",
    "timekeeping",
    "scheduling",
    "accruals",
    "leave",
    "attendance",
    "attestations",
    "work",
    "payroll",
    "forecasting",
}


def test_every_capability_family_present():
    families = {r.family for r in RESOURCE_REGISTRY.values()}
    assert EXPECTED_FAMILIES <= families


def test_payroll_is_async_export():
    assert get_resource("payroll_export").incremental_style == IncrementalStyle.ASYNC_EXPORT


def test_net_change_resource_has_50_batch_limit():
    r = get_resource("work_activity_net_changes")
    assert r.incremental_style == IncrementalStyle.NET_CHANGE
    assert r.batch_limit == 50


def test_work_activity_shifts_is_hyperfind_date_window():
    r = get_resource("work_activity_shifts")
    assert r.family == "work"
    assert r.employee_scope == EmployeeScope.HYPERFIND
    assert r.incremental_style == IncrementalStyle.DATE_WINDOW
    assert r.pagination == PaginationStyle.MULTI_READ
    assert r.primary_key == []


def test_persons_is_org_wide_apply_read():
    r = get_resource("persons")
    assert r.endpoint_path == "/commons/persons/apply_read"
    assert r.body_style == BodyStyle.APPLY_READ_PERSONS
    assert r.pagination == PaginationStyle.APPLY_READ
    assert r.employee_scope == EmployeeScope.NONE
    assert r.page_count == 1000
    assert r.records_key == "records"
    assert r.primary_key == ["personNumber"]


def test_punches_is_apply_read_with_minute_window():
    r = get_resource("timekeeping_punches")
    assert r.endpoint_path == "/timekeeping/punches/apply_read"
    assert r.body_style == BodyStyle.APPLY_READ_PUNCHES
    assert r.pagination == PaginationStyle.APPLY_READ
    assert r.page_count == 25
    assert r.window_max_minutes == 60
    assert r.records_key == "data"


def test_hyperfind_scope_resources_flagged():
    assert get_resource("timekeeping_punches").employee_scope == EmployeeScope.HYPERFIND


def test_unknown_resource_raises():
    with pytest.raises(UserException):
        get_resource("does_not_exist")
