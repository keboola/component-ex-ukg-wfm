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
    client_id: str = Field(alias="#client_id")
    client_secret: str = Field(alias="#client_secret")
    username: str = Field(alias="#username")
    password: str = Field(alias="#password")

    # --- row (per resource) ---
    resource: str | None = None
    load_type: LoadType = LoadType.INCREMENTAL
    since: str | None = None
    # Optional upper bound of the fetch window; empty = now (run start).
    until: str | None = None
    # Numeric symbolic-period id (as a string, e.g. "1" = Current Pay Period) from
    # GET /commons/symbolicperiod — a rolling window that replaces since/until. Pick it with the
    # Symbolic Period dropdown; WFM rejects a qualifier name (WTK-147500). Empty = use since/until.
    symbolic_period: str | None = None
    hyperfind_ref: str | None = None
    # Max employees a Hyperfind may resolve before UKG rejects hyperfind/execute with WCO-112003.
    # UKG's per-request default is low, so a broad Hyperfind ("All Home"/"All People") 400s unless
    # we raise the cap. 50000 mirrors the value proven in production for a full-org roster.
    hyperfind_threshold: int = Field(default=50000, ge=1)
    select: list[str] = Field(default_factory=list)
    # Timecard-metrics-only picker: the API `select` groups chosen via the list_timecard_metrics
    # dropdown (a separate row-schema field so it shows only for that resource). Folded into the
    # effective select below; `select` (free-text, other resources) takes precedence if both are set.
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
    def effective_select(self) -> list[str]:
        """API `select` groups: the free-text `select` if set, else the timecard-metrics picker."""
        return self.select or self.metric_groups
