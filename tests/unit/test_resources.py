import json
from pathlib import Path

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

_ROW_SCHEMA = Path(__file__).parents[2] / "component_config" / "configRowSchema.json"

EXPECTED_FAMILIES = {
    "people",
    "business_structure",
    "hyperfind",
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


def test_row_schema_resource_enum_equals_registry_keys():
    """Gate: the configRowSchema `resource` enum must EXACTLY match the registry keys (same set
    and order), and enum_titles must stay index-aligned. Skipped where component_config/ is not
    checked out (the runtime Docker image ships only src/scripts/tests, not portal metadata)."""
    if not _ROW_SCHEMA.exists():
        pytest.skip("component_config/configRowSchema.json not present in this environment")
    schema = json.loads(_ROW_SCHEMA.read_text())
    resource_prop = schema["properties"]["resource"]
    enum = resource_prop["enum"]
    titles = resource_prop["options"]["enum_titles"]
    assert enum == list(RESOURCE_REGISTRY.keys())
    assert len(titles) == len(enum)


def test_dropped_resources_absent():
    for dropped in ("information_access", "leave_requests", "attendance_patterns"):
        assert dropped not in RESOURCE_REGISTRY


def test_accruals_share_timecard_metrics_endpoint_via_select():
    balances = get_resource("accruals_balances")
    transactions = get_resource("accruals_transactions")
    summaries = get_resource("accruals_summaries")
    for r in (balances, transactions, summaries):
        assert r.family == "accruals"
        assert r.endpoint_path == "/timekeeping/timecard_metrics/multi_read"
        assert r.body_style == BodyStyle.EMPLOYEE_SET_METRICS
        assert r.primary_key == ["employeeId_id"]
    assert balances.select == ["ACCRUAL_SUMMARY"]
    assert summaries.select == ["ACCRUAL_SUMMARY"]
    assert transactions.select == ["ACCRUAL_TRANSACTIONS"]


def test_business_structure_uses_legacy_locations():
    r = get_resource("business_structure")
    assert r.endpoint_path == "/commons/locations/multi_read"
    assert r.body_style == BodyStyle.LOCATIONS_QUERY
    assert r.primary_key == ["nodeId"]


def test_scheduling_swaps_shape():
    r = get_resource("scheduling_swaps")
    assert r.endpoint_path == "/scheduling/employee_swap/multi_read"
    assert r.body_style == BodyStyle.SWAP_EMPLOYEES
    assert r.employee_scope == EmployeeScope.HYPERFIND
    assert r.incremental_style == IncrementalStyle.DATE_WINDOW


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
