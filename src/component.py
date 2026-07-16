import csv
import logging
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition, SupportedDataTypes
from keboola.component.exceptions import UserException

from client.orchestration import iter_records
from client.payroll import run_async_export
from client.resources import IncrementalStyle, ResourceDef, effective_incremental, get_resource
from client.transform import flatten_record
from client.wfm_client import WfmClient
from client.window import STATE_LAST_RUN, resolve_window
from configuration import Configuration

_TIMESTAMP_FIELDS = {
    "createdDateTime",
    "updatedDateTime",
    "startDateTime",
    "endDateTime",
    "start",
    "end",
}


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()
        self._config = Configuration(**self.configuration.parameters)
        self.client = WfmClient(
            host=self._config.host,
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            username=self._config.username,
            password=self._config.password,
        )

    def run(self) -> None:
        if self._config.resource is None:
            raise UserException("'resource' is required. Configure a resource row.")
        resource = get_resource(self._config.resource)
        state = self.get_state_file() or {}
        since_iso, until_iso, run_started = self._compute_window(resource, state)
        record_iter = self._record_source(resource, since_iso, until_iso)
        row_count, columns = self._stream_and_write_table(resource, record_iter)
        if row_count == 0:
            logging.info("No rows returned for resource '%s'; skipping table write.", resource.name)
        if self._effective_incremental(resource):
            # Advance watermark even on empty result to prevent unbounded window growth.
            self.write_state_file({STATE_LAST_RUN: run_started})
        if row_count:
            logging.info("Extracted resource '%s': %s rows, %s columns.", resource.name, row_count, len(columns))

    def _effective_incremental(self, resource: ResourceDef) -> bool:
        """The one predicate governing watermark, fetch window, and manifest flag (see resources)."""
        return effective_incremental(resource, self._config.incremental)

    def _compute_window(self, resource: ResourceDef, state: dict[str, Any]) -> tuple[str | None, str | None, str]:
        """Return (since_iso, until_iso, run_started_iso)."""
        # A symbolic period replaces the date window; skip window and watermark logic.
        if self._config.symbolic_period:
            return None, None, datetime.now(UTC).isoformat()
        date_field = self._config.date_field or resource.date_field
        if not date_field:
            return None, None, datetime.now(UTC).isoformat()
        return resolve_window(
            state,
            date_field,
            self._config.since,
            self._config.overlap_margin_seconds,
            self._effective_incremental(resource),
        )

    def _record_source(
        self, resource: ResourceDef, since_iso: str | None, until_iso: str | None
    ) -> Iterator[dict[str, Any]]:
        if resource.incremental_style == IncrementalStyle.ASYNC_EXPORT:
            return run_async_export(
                self.client,
                resource,
                since_iso or "",
                until_iso or "",
                self._config.hyperfind_ref,
                max_wait_s=self._config.max_wait_seconds,
                poll_interval_s=self._config.poll_interval_seconds,
            )
        return iter_records(
            self.client,
            resource,
            hyperfind_ref=self._config.hyperfind_ref,
            since_iso=since_iso,
            until_iso=until_iso,
            select=self._config.select,
            symbolic_period=self._config.symbolic_period,
        )

    def _stream_and_write_table(
        self, resource: ResourceDef, record_iter: Iterator[dict[str, Any]]
    ) -> tuple[int, list[str]]:
        """Stream rows to the output table with deterministic sorted columns.

        Two-phase approach using a disk-backed temp file:
          Phase 1 — rows are written one-at-a-time to a temp file using an
                     insertion-order fieldnames list that grows as new columns arrive.
                     Peak RAM: the column name set + one flattened row dict at a time.
          Phase 2 — the temp file is re-read row-by-row and stream-copied to the
                     final out-table path with a sorted, normalised header.
                     Peak RAM: unchanged — column name set + one row dict at a time.

        The full dataset is never held in memory. The temp file lives in /tmp (tempfile
        default), never under data/out/tables/.
        """
        seen_columns: dict[str, None] = {}  # insertion-order set for dedup; sorted at write time
        row_count = 0

        # Phase 1: stream rows into a disk temp file.
        # extrasaction='ignore' + restval='' handle sparse rows (columns seen only on later rows
        # are back-filled as empty strings when the final writer re-emits with restval='').
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="", suffix=".csv") as tmp:
            deferred_writer: csv.DictWriter | None = None
            for record in record_iter:
                row = flatten_record(record)
                for key in row:
                    seen_columns[key] = None
                if deferred_writer is None:
                    # Create writer on first row; fieldnames extended below as new columns arrive
                    deferred_writer = csv.DictWriter(
                        tmp,
                        fieldnames=list(seen_columns),
                        extrasaction="ignore",
                        restval="",
                    )
                elif set(row.keys()) - set(deferred_writer.fieldnames):
                    # New columns encountered — extend fieldnames for subsequent rows
                    deferred_writer.fieldnames = list(seen_columns)
                deferred_writer.writerow(row)
                row_count += 1

            if row_count == 0:
                return 0, []

            # Phase 2: sorted columns → deterministic manifest schema across incremental runs
            columns = sorted(seen_columns)
            schema = {
                col: ColumnDefinition(
                    data_types=BaseType(
                        dtype=(SupportedDataTypes.TIMESTAMP if col in _TIMESTAMP_FIELDS else SupportedDataTypes.STRING)
                    ),
                    nullable=True,
                    primary_key=col in resource.primary_key,
                )
                for col in columns
            }
            # Same predicate as the watermark and window logic: incremental append/upsert
            # only with a stable PK; a keyless resource always full-REPLACEs.
            is_incremental = self._effective_incremental(resource)
            table = self.create_out_table_definition(
                f"{resource.name}.csv",
                primary_key=resource.primary_key,
                incremental=is_incremental,
                has_header=True,
                schema=schema,
            )

            # Rewind temp file, then stream-copy one row at a time into the final out-table path.
            # The reader uses insertion-order fieldnames so each dict maps correctly to values;
            # the writer re-emits with sorted fieldnames (extrasaction='ignore', restval='').
            tmp.seek(0)
            reader = csv.DictReader(tmp, fieldnames=list(seen_columns))
            with open(table.full_path, "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", restval="")
                writer.writeheader()
                for row in reader:
                    writer.writerow(row)

        self.write_manifest(table)
        return row_count, columns

    @sync_action("testConnection")
    def test_connection(self) -> dict[str, str]:
        self.client.get_token()
        return {"status": "success"}


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
