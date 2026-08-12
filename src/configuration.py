from enum import StrEnum
from typing import Any

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field, ValidationError, computed_field


class LoadType(StrEnum):
    FULL = "full_load"
    INCREMENTAL = "incremental_load"


class Configuration(BaseModel):
    model_config = {"extra": "ignore", "populate_by_name": True}

    # --- root (global auth) ---
    host: str
    # client_id and the service-account username are non-secret identifiers — plain (visible) config
    # fields, no `#` prefix. Only client_secret and password stay encrypted; neither pair authenticates
    # without the other.
    client_id: str
    client_secret: str = Field(alias="#client_secret")
    username: str
    password: str = Field(alias="#password")

    # --- row (per resource) ---
    resource: str | None = None
    load_type: LoadType = LoadType.INCREMENTAL
    since: str | None = None
    # Optional upper bound of the fetch window; empty = now (run start).
    until: str | None = None
    # Max span (in days) of each date sub-window when a [Start, End] pull is chunked. Lower it to
    # fetch a large window in smaller pieces so peak memory stays under the component limit — each
    # sub-window is a separate request whose response is parsed on its own. Empty = the per-resource
    # default (365 days). Ignored by punch-level resources, which always use their own minute cap,
    # and by non-date-windowed resources.
    window_days: int | None = Field(default=None, ge=1)
    # UI discriminator for how the fetch window is chosen: "date_window" (Start/End Date) or
    # "symbolic_period" (a rolling UKG period). It gates which fields the form shows AND is
    # authoritative in code (see effective_symbolic_period) so a hidden, stale symbolic_period value
    # can't drive a run. None = a pre-window_type config; the legacy "symbolic_period presence wins"
    # rule then applies, preserving existing configs.
    window_type: str | None = None
    # Numeric symbolic-period id (as a string, e.g. "1" = Current Pay Period) from
    # GET /commons/symbolicperiod — a rolling window that replaces since/until. Pick it with the
    # Symbolic Period dropdown; WFM rejects a qualifier name (WTK-147500). Empty = use since/until.
    symbolic_period: str | None = None
    hyperfind_ref: str | None = None
    # Max employees a Hyperfind may resolve before UKG rejects hyperfind/execute with WCO-112003.
    # UKG's per-request default is low, so a broad Hyperfind ("All Home"/"All People") 400s unless
    # we raise the cap. 50000 mirrors the value proven in production for a full-org roster.
    hyperfind_threshold: int = Field(default=50000, ge=1)
    # Advanced API `select` passthrough (the fields / metric tokens a resource requests). No longer
    # exposed in the config UI: it was undocumented, and where a resource's registry default is
    # load-bearing (accruals, attestations, work / net-change) a user override silently broke the
    # read. Kept for back-compat / raw-JSON power users; empty = the resource's registry default.
    select: list[str] = Field(default_factory=list)
    # Timecard-metrics-only picker: the ONE metric group (API `select` token) chosen from the static
    # single-select dropdown. Authoritative for timecard_metrics' effective select. `metric_groups`
    # is retained only to fold a pre-explode (multi-select) config to its first element; new configs
    # use metric_group.
    metric_group: str | None = None
    metric_groups: list[str] = Field(default_factory=list)
    # User-supplied primary key for the output table. Overrides the resource registry default and,
    # on incremental load, enables upsert even for a registry-keyless resource.
    primary_key: list[str] = Field(default_factory=list)
    max_wait_seconds: int = Field(default=1800, ge=1)
    poll_interval_seconds: int = Field(default=15, ge=1)
    # Tenant-defined SQL-like query for the async payroll export (payroll_export resource only).
    payroll_query: str | None = None
    # Sampling knobs (advanced/testing): override the apply_read page size and cap the number of
    # pages fetched. Deliberately NOT exposed in the config UI (they looked out of place); set them
    # via raw config JSON if a run needs to be bounded. Both default None so production is unaffected.
    page_size: int | None = Field(default=None, ge=1)
    max_pages: int | None = Field(default=None, ge=1)
    # Employees per multi_read request; overrides the per-resource registry default. Lower it when a
    # resource with a large per-employee payload risks the 256 MB memory limit (the whole batch
    # response is parsed at once), raise it to cut request count. Default None = registry value.
    batch_size: int | None = Field(default=None, ge=1)

    def __init__(self, **data: Any):
        try:
            super().__init__(**data)
        except ValidationError as e:
            messages = [f"{err['loc'][0] if err['loc'] else 'unknown'}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(messages)}") from e

    @computed_field
    @property
    def incremental(self) -> bool:
        return self.load_type == LoadType.INCREMENTAL

    @computed_field
    @property
    def effective_symbolic_period(self) -> str | None:
        """The symbolic period that actually drives the run, honoring the window_type mode picker.

        `window_type` is authoritative: in "date_window" mode a stale/hidden `symbolic_period` value
        is ignored (returns None → the Start/End window applies). A pre-window_type config
        (window_type is None) keeps the legacy behaviour where a set `symbolic_period` wins.
        """
        if self.window_type == "date_window":
            return None
        return self.symbolic_period or None

    @computed_field
    @property
    def effective_select(self) -> list[str]:
        """API `select` groups. For timecard_metrics the single metric-group picker (`metric_group`)
        is authoritative; a stale/raw `select` must not override it, and a legacy multi-value
        `metric_groups` folds to its first element (back-compat). An empty picker yields an empty
        select — the component rejects that for timecard_metrics (it needs exactly one section to
        explode). Every other resource falls back to the free-text `select`.
        """
        if self.resource == "timekeeping_timecard_metrics":
            if self.metric_group:
                return [self.metric_group]
            if self.metric_groups:
                return [self.metric_groups[0]]
            return []
        return self.select
