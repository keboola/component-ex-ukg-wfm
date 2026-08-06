"""Shared pytest fixtures.

keboola.datadirtest's VCR tester monkeypatches ``ComponentBase._should_vcr_replay`` /
``_should_vcr_record`` while it drives cassette replay. In a single-process ``pytest tests/`` run
(which is what CI and the Docker ``test`` stage do), a leaked replacement makes a *later* non-VCR
datadir run fail with ``_should_vcr_replay() takes 0 positional arguments but 1 was given`` — the
patched hook gets called as an instance method and receives ``self``. That turns a clean
``UserException`` (exit 1) into an unexpected exit 2 in the mock suite, but only when the VCR suite
runs first.

Snapshot the pristine class hooks at import (before any test patches them) and restore them after
every test, so the functional/VCR suite can't contaminate the mock/datadir suite regardless of
collection order.
"""

import pytest
from keboola.component.base import ComponentBase

_VCR_HOOKS = ("_should_vcr_replay", "_should_vcr_record")
_ORIGINAL_HOOKS = {name: ComponentBase.__dict__.get(name) for name in _VCR_HOOKS}


@pytest.fixture(autouse=True)
def _restore_component_vcr_hooks():
    """Restore ComponentBase VCR hooks after each test to prevent cross-suite monkeypatch leaks."""
    yield
    for name, original in _ORIGINAL_HOOKS.items():
        if original is not None:
            setattr(ComponentBase, name, original)
