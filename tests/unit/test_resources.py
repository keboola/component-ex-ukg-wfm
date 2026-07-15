import pytest
from keboola.component.exceptions import UserException

from client.resources import (
    RESOURCE_REGISTRY,
    EmployeeScope,
    IncrementalStyle,
    get_resource,
)

EXPECTED_FAMILIES = {
    "people", "business_structure", "hyperfind", "information_access",
    "timekeeping", "scheduling", "accruals", "leave", "attendance",
    "attestations", "work", "payroll", "forecasting",
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


def test_persons_batch_limit_100():
    assert get_resource("persons").batch_limit == 100


def test_hyperfind_scope_resources_flagged():
    assert get_resource("timekeeping_punches").employee_scope == EmployeeScope.HYPERFIND


def test_unknown_resource_raises():
    with pytest.raises(UserException):
        get_resource("does_not_exist")
