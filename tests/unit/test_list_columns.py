"""Guardrail tests for the list_columns sync action.

The @sync_action wrapper adds stdout redirection / result serialization keyed off
self.configuration.action; we exercise the underlying method via __wrapped__ so the tests cover
the guard logic and the user-facing messages directly. Component.__new__ skips __init__ (which would
build a WfmClient); list_columns only reads self._config.resource + self.environment_variables.
"""

from types import SimpleNamespace

import pytest
from keboola.component.exceptions import UserException

import component as component_module
from component import Component

_raw = Component.list_columns.__wrapped__


def _comp(
    resource="persons",
    token="tok",
    url="https://connection.keboola.com",
    component_id="keboola.ukg-wfm",
    config_id="123",
):
    comp = Component.__new__(Component)
    comp._config = SimpleNamespace(resource=resource)
    comp.environment_variables = SimpleNamespace(token=token, url=url, component_id=component_id, config_id=config_id)
    return comp


def test_requires_resource():
    with pytest.raises(UserException, match="Select a resource"):
        _raw(_comp(resource=None))


def test_requires_forwarded_token():
    with pytest.raises(UserException, match="token"):
        _raw(_comp(token=None))


def test_requires_component_and_config_id():
    with pytest.raises(UserException, match="saved and has run once"):
        _raw(_comp(config_id=None))


def test_missing_table_reports_run_once(monkeypatch):
    monkeypatch.setattr(component_module, "get_table_columns", lambda url, token, table_id: None)
    with pytest.raises(UserException, match="No output table found"):
        _raw(_comp())


def test_resolves_default_bucket_table_id(monkeypatch):
    captured = {}

    def fake(url, token, table_id):
        captured["table_id"] = table_id
        return ["a"]

    monkeypatch.setattr(component_module, "get_table_columns", fake)
    _raw(_comp(component_id="keboola.ukg-wfm", config_id="999"))
    assert captured["table_id"] == "in.c-keboola-ukg-wfm-999.persons"


def test_returns_select_elements(monkeypatch):
    monkeypatch.setattr(
        component_module, "get_table_columns", lambda url, token, table_id: ["personNumber", "firstName"]
    )
    result = _raw(_comp())
    # Columns are returned alphabetized (case-insensitive) so the PK picker is scannable.
    assert [e.value for e in result] == ["firstName", "personNumber"]
    assert all(e.label == e.value for e in result)
