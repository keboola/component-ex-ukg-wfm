import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration, LoadType


def _root():
    return {
        "host": "https://acme.prd.mykronos.com",
        "client_id": "c",
        "#client_secret": "s",
        "username": "u",
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
