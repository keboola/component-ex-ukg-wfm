"""Unit tests for the per-resource sticky column state (see Component._STATE_COLUMNS_KEY).

Incremental Storage loads reject a column set narrower than the existing table's, so the component
persists every column ever emitted for a resource in state.json (key "schema_columns") and re-emits
their union each run -- the schema only ever grows.

Component.__new__ skips __init__ (which would build a WfmClient); _load_sticky_columns and
_extend_sticky_columns only touch get_state_file/write_state_file, which we stub directly on the
instance -- same pattern as tests/unit/test_list_columns.py.
"""

from types import SimpleNamespace

from component import Component

_RESOURCE = SimpleNamespace(name="accruals_balances")


def _comp(state):
    comp = Component.__new__(Component)
    comp.get_state_file = lambda: state
    return comp


def test_extend_sticky_columns_first_run_returns_just_this_runs_columns():
    # No prior state (first run) -- the sticky set is exactly what this run saw, sorted.
    comp = _comp({})
    written: list[dict] = []
    comp.write_state_file = written.append

    result = comp._extend_sticky_columns(_RESOURCE, ["c", "a", "b"])

    assert result == ["a", "b", "c"]
    assert written == [{"schema_columns": {"accruals_balances": ["a", "b", "c"]}}]


def test_extend_sticky_columns_unions_with_prior_and_never_shrinks():
    # Prior runs saw column "d" (an optional field); this run's data omits it. The sticky set must
    # still include "d" so the Storage upsert never sees a narrower column set than before.
    prior_state = {"schema_columns": {"accruals_balances": ["a", "b", "c", "d"]}}
    comp = _comp(prior_state)
    written: list[dict] = []
    comp.write_state_file = written.append

    result = comp._extend_sticky_columns(_RESOURCE, ["a", "b", "c"])

    assert result == ["a", "b", "c", "d"]
    assert written == [{"schema_columns": {"accruals_balances": ["a", "b", "c", "d"]}}]


def test_extend_sticky_columns_preserves_other_resources_and_state_keys():
    # A single state read + write must not clobber another resource's sticky set or unrelated keys.
    prior_state = {
        "schema_columns": {"other_resource": ["x", "y"]},
        "some_other_key": "kept",
    }
    comp = _comp(prior_state)
    written: list[dict] = []
    comp.write_state_file = written.append

    comp._extend_sticky_columns(_RESOURCE, ["a", "b"])

    assert written[0]["some_other_key"] == "kept"
    assert written[0]["schema_columns"]["other_resource"] == ["x", "y"]
    assert written[0]["schema_columns"]["accruals_balances"] == ["a", "b"]


def test_load_sticky_columns_returns_empty_when_no_prior_state():
    comp = _comp({})
    assert comp._load_sticky_columns(_RESOURCE) == []


def test_load_sticky_columns_returns_persisted_columns_for_this_resource_only():
    prior_state = {"schema_columns": {"accruals_balances": ["a", "b", "c"], "other": ["z"]}}
    comp = _comp(prior_state)
    assert comp._load_sticky_columns(_RESOURCE) == ["a", "b", "c"]
