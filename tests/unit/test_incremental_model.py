"""The incremental predicate governs the Storage write mode only.

`effective_incremental(resource, incremental_load, config_pk)` is True ONLY when the user chose
incremental_load AND the resource has a stable primary key (registry default or user-supplied). A
keyless resource in incremental_load must behave as a full REPLACE — an incremental append without a
PK would duplicate rows unboundedly.

This predicate drives the manifest `incremental` flag and PK upsert; it does NOT touch the fetch
window. The window is config-driven (Start/End Date) and recomputed every run, with no state
watermark — see tests/unit/test_window.py.
"""

from client.resources import effective_incremental, get_resource


def test_effective_incremental_true_only_with_pk_and_incremental_load():
    persons = get_resource("persons")  # has a primary key
    assert effective_incremental(persons, incremental_load=True) is True
    assert effective_incremental(persons, incremental_load=False) is False


def test_effective_incremental_false_for_keyless_resource_even_in_incremental_load():
    keyless = get_resource("attestations")  # primary_key == []
    assert keyless.primary_key == []
    assert effective_incremental(keyless, incremental_load=True) is False


def test_effective_incremental_true_when_user_pk_supplied_on_keyless_resource():
    """A user-supplied primary_key enables incremental upsert even for a registry-keyless resource."""
    keyless = get_resource("attestations")
    assert keyless.primary_key == []
    assert effective_incremental(keyless, incremental_load=True, config_pk=["id"]) is True
    # Still gated by load type: full_load with a user PK is not effectively incremental.
    assert effective_incremental(keyless, incremental_load=False, config_pk=["id"]) is False


def test_effective_incremental_false_when_no_pk_anywhere():
    """Keyless resource + incremental_load + no user PK stays full-replace (non-breaking)."""
    keyless = get_resource("attestations")
    assert effective_incremental(keyless, incremental_load=True, config_pk=[]) is False
