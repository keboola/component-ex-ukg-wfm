"""The single incremental model (B1): one predicate governs watermark, window, manifest.

`effective_incremental(resource, incremental_load)` must be True ONLY when the user chose
incremental_load AND the resource has a stable primary key. A keyless resource in
incremental_load must behave as a full REPLACE every run: it must NOT read/advance the
state watermark, and its fetch window must be [since, now] on every run (no shrink, no
data loss). This is the regression that motivated unifying the three predicates.
"""

from client.resources import effective_incremental, get_resource
from client.window import STATE_LAST_RUN, resolve_window


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


def test_keyless_incremental_window_ignores_watermark_uses_since_every_run():
    """Regression: a keyless incremental_load resource must NOT shrink its window on run 2.

    Even with a later watermark sitting in state, an *effectively non-incremental* run must
    ignore it and re-fetch the full [since, now] window, so the full-REPLACE load never loses
    the older rows the shrunken watermark window would have dropped.
    """
    keyless = get_resource("attestations")
    since = "2026-01-01T00:00:00+00:00"
    # Run 2 state: a watermark far later than `since` (what the buggy code would shrink to).
    state = {STATE_LAST_RUN: "2026-06-01T00:00:00+00:00"}

    since_iso, until_iso, _ = resolve_window(
        state,
        keyless.date_field or "start",
        since,
        is_effective_incremental=effective_incremental(keyless, incremental_load=True),
    )

    assert since_iso == since  # uses `since`, not the 2026-06-01 watermark → no window shrink
    assert until_iso is not None


def test_effective_incremental_window_uses_watermark():
    """The HR-style path (PK + incremental_load) reads the watermark as the lower bound verbatim."""
    persons = get_resource("persons")
    state = {STATE_LAST_RUN: "2026-06-01T00:00:00+00:00"}

    since_iso, _, _ = resolve_window(
        state,
        "start",
        since=None,
        is_effective_incremental=effective_incremental(persons, incremental_load=True),
    )

    assert since_iso == "2026-06-01T00:00:00+00:00"  # watermark used as-is (no overlap)
