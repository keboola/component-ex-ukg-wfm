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


def test_effective_select_prefers_explicit_select():
    # Free-text select (other resources / power users) wins over the picker if both are set.
    cfg = Configuration(**_root(), resource="persons", select=["FOO"], metric_groups=["ACTUAL_TOTALS"])
    assert cfg.effective_select == ["FOO"]


def test_effective_select_empty_by_default():
    cfg = Configuration(**_root(), resource="persons")
    assert cfg.effective_select == []
