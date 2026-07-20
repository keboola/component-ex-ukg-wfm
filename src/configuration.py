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
    overlap_margin_seconds: int = Field(default=0, ge=0)
    symbolic_period: str | None = None
    hyperfind_ref: str | None = None
    select: list[str] = Field(default_factory=list)
    date_field: str | None = None
    max_wait_seconds: int = Field(default=1800, ge=1)
    poll_interval_seconds: int = Field(default=15, ge=1)
    # Tenant-defined SQL-like query for the async payroll export (payroll_export resource only).
    payroll_query: str | None = None
    # Sampling knobs (advanced/testing): override the apply_read page size and cap the number of
    # pages fetched. Both default None so production behaviour is unaffected.
    page_size: int | None = Field(default=None, ge=1)
    max_pages: int | None = Field(default=None, ge=1)

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
