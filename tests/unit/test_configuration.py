import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration, LoadType


def _root():
    return {
        "host": "https://acme.prd.mykronos.com",
        "#client_id": "c",
        "#client_secret": "s",
        "#username": "u",
        "#password": "p",
    }


def test_secrets_and_host_parse():
    cfg = Configuration(**_root(), resource="persons")
    assert cfg.host.startswith("https://")
    assert cfg.client_id == "c"
    assert cfg.password == "p"
    assert cfg.incremental is True


def test_full_load_flag():
    cfg = Configuration(**_root(), resource="persons", load_type="full_load")
    assert cfg.load_type == LoadType.FULL
    assert cfg.incremental is False


def test_missing_host_raises_userexception():
    data = _root()
    del data["host"]
    with pytest.raises(UserException):
        Configuration(**data, resource="persons")


def test_effective_select_folds_metric_groups():
    # The timecard-metrics picker (metric_groups) feeds the API select when set.
    cfg = Configuration(**_root(), resource="timekeeping_timecard_metrics", metric_groups=["ACTUAL_TOTALS"])
    assert cfg.effective_select == ["ACTUAL_TOTALS"]


def test_non_metrics_resource_uses_free_text_select():
    # Every non-metrics resource uses the free-text `select`; metric_groups is irrelevant there.
    cfg = Configuration(**_root(), resource="persons", select=["FOO"], metric_groups=["ACTUAL_TOTALS"])
    assert cfg.effective_select == ["FOO"]


def test_metric_groups_wins_over_stale_select_for_timecard_metrics():
    # Regression: a stale/hidden `select` on the timecard_metrics row must NOT override the picker.
    cfg = Configuration(
        **_root(),
        resource="timekeeping_timecard_metrics",
        select=["SCHEDULED_TOTALS", "PROJECTED_TOTALS"],  # stale leftover, hidden for this resource
        metric_groups=["ACTUAL_TOTALS"],
    )
    assert cfg.effective_select == ["ACTUAL_TOTALS"]


def test_empty_metric_groups_means_all_sections_ignoring_stale_select():
    # Empty picker on timecard_metrics = all sections; a stale `select` must not leak back in.
    cfg = Configuration(
        **_root(), resource="timekeeping_timecard_metrics", select=["SCHEDULED_TOTALS"], metric_groups=[]
    )
    assert cfg.effective_select == []


def test_effective_select_empty_by_default():
    cfg = Configuration(**_root(), resource="persons")
    assert cfg.effective_select == []


def test_window_type_date_ignores_stale_symbolic_period():
    # The mode picker is authoritative: in date_window mode a hidden/stale symbolic_period value
    # must NOT drive the run — it resolves to None so the Start/End window applies.
    cfg = Configuration(**_root(), resource="persons", window_type="date_window", symbolic_period="1")
    assert cfg.effective_symbolic_period is None


def test_window_type_symbolic_uses_symbolic_period():
    cfg = Configuration(**_root(), resource="persons", window_type="symbolic_period", symbolic_period="1")
    assert cfg.effective_symbolic_period == "1"


def test_legacy_config_without_window_type_keeps_symbolic_period():
    # Back-compat: a pre-window_type config (window_type absent → None) still honors a set
    # symbolic_period, so existing configs are unchanged.
    cfg = Configuration(**_root(), resource="persons", symbolic_period="1")
    assert cfg.window_type is None
    assert cfg.effective_symbolic_period == "1"


def test_symbolic_mode_without_a_period_resolves_to_none():
    cfg = Configuration(**_root(), resource="persons", window_type="symbolic_period")
    assert cfg.effective_symbolic_period is None
