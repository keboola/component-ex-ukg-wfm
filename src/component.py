import csv
import logging
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition, SupportedDataTypes
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement

from client.orchestration import iter_records
from client.payroll import run_async_export
from client.resources import (
    IncrementalStyle,
    ResourceDef,
    effective_incremental,
    effective_primary_key,
    get_resource,
)
from client.storage import default_output_table_id, get_table_columns
from client.transform import flatten_record
from client.wfm_client import WfmClient
from client.window import STATE_LAST_RUN, resolve_window
from configuration import Configuration

# Baseline recording sanitizer: DefaultSanitizer strips the Authorization header and the known
# credential fields (client_id, client_secret, password, token, access_token, refresh_token) from
# recorded cassettes; `username` is a WFM credential the default list misses. On top of that we
# redact broadly — every identifying / free-text field WFM returns (names, labels, descriptions,
# comments) — keeping only numeric ids, dates, numbers, booleans and enums, which are the
# structural data that prove the extraction without exposing employee PII. Host rewriting maps the
# real tenant host to a placeholder, and a CallbackSanitizer caps record volume so cassettes stay
# small.
#
# scrub_before_read (keboola.vcr >= 0.7.0): tagging a sanitizer makes it redact the HTTP response
# BEFORE the component consumes it at record time (in addition to the cassette), so expected/
# tables, logs.json and output_snapshot.json are captured already-scrubbed and match replay — no
# post-hoc regeneration needed. We tag ONLY the identity/free-text redaction (BodyFieldSanitizer)
# and the array cap; credentials, the OAuth token and numeric employee ids must stay REAL while the
# component runs (it round-trips the token into the Authorization header and hyperfind ids into
# later request bodies — a pre-read placeholder there trips the recorder's round-trip guard), so
# DefaultSanitizer and the host rewrite stay cassette-only.
#
# keboola.vcr is a dev-only dependency (via keboola.datadirtest); the production image is built with
# `uv sync --no-dev`, so guard the import — VCR_SANITIZERS is only consumed by the recording harness.
try:
    import json as _json

    from keboola.vcr import BodyFieldSanitizer, CallbackSanitizer, DefaultSanitizer, UrlPatternSanitizer

    _MAX_ARRAY_ITEMS = 25

    # Identifying / free-text fields WFM returns. Redacted both in the cassette (DefaultSanitizer)
    # and before the component reads the response (BodyFieldSanitizer, scrub_before_read). No numeric
    # ids here — those are round-tripped into later requests and must survive recording.
    _IDENTITY_FIELDS = [
        # Credential / person identity (already vetted).
        "username",
        "firstName",
        "lastName",
        "fullName",
        "displayName",
        "updateByPersonFullName",
        "personNumber",
        # Identifying names and free-text fields across WFM resources.
        "name",
        "qualifier",
        "shortName",
        "typeName",
        "description",
        "functionalAreaName",
        "parentName",
        "holidayDisplayName",
        "dataSourceDisplayName",
        "label",
        "trackingLabel",
        "laborCategoryEntryDescription",
        "commentNotes",
        "commentsNotes",
        "comments",
        "comment",
        "notes",
        # Org-path / hierarchy locators and free-text question/answer/message fields.
        "path",
        "parentPath",
        "orgPath",
        # persistentId carries human-readable facility/department labels (e.g. Hyperfind
        # query keys) that identify the tenant's sites — treat as identifying, not a bare id.
        "persistentId",
        "scope",
        "question",
        "shortQuestion",
        "answer",
        "message",
    ]

    def _truncate_json_arrays(value: Any) -> Any:
        """Recursively cap every JSON array to at most _MAX_ARRAY_ITEMS elements."""
        if isinstance(value, list):
            return [_truncate_json_arrays(item) for item in value[:_MAX_ARRAY_ITEMS]]
        if isinstance(value, dict):
            return {key: _truncate_json_arrays(item) for key, item in value.items()}
        return value

    def _cap_response_records(response: dict) -> dict:
        """CallbackSanitizer before_response hook: shrink recorded responses.

        Receives the vcrpy response dict (body at response["body"]["string"] as
        str or bytes), JSON-parses it, truncates every array, and re-serializes
        to valid JSON so the replay parser still reads it. Non-JSON bodies pass
        through untouched.
        """
        body = response.get("body")
        if not isinstance(body, dict) or "string" not in body:
            return response
        raw = body["string"]
        is_bytes = isinstance(raw, bytes)
        text = raw.decode("utf-8", errors="ignore") if is_bytes else raw
        if not text:
            return response
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError, TypeError, ValueError:
            return response
        capped = _json.dumps(_truncate_json_arrays(data))
        body["string"] = capped.encode("utf-8") if is_bytes else capped
        return response

    VCR_SANITIZERS = [
        # Cassette-only: redact credentials (defaults) + the identity fields in the recorded
        # request/response. Credentials stay REAL while the component runs (the token is
        # round-tripped into the Authorization header), redacted only when written to the cassette.
        DefaultSanitizer(additional_sensitive_fields=_IDENTITY_FIELDS),
        # Pre-read: redact the identity/free-text fields in the response BEFORE the component reads
        # it, so expected/ tables, logs.json and output_snapshot.json are natively scrubbed and
        # match replay. Body-only (no header filtering) and never touches numeric ids/tokens, so
        # nothing the component round-trips into a later request is lost.
        BodyFieldSanitizer(fields=_IDENTITY_FIELDS, nested=True, scrub_before_read=True),
        UrlPatternSanitizer(patterns=[(r"[a-z0-9-]+\.prd\.mykronos\.com", "acme.prd.mykronos.com")]),
        # Pre-read cap so the component reads the same <=25-item arrays that replay will, keeping
        # logged row counts and snapshot hashes identical between record and replay.
        CallbackSanitizer(before_response=_cap_response_records, scrub_before_read=True),
    ]
except ImportError:
    VCR_SANITIZERS = []

# Some WFM responses (e.g. timecard_metrics) flatten into very large single fields that exceed
# Python's default 128 KB csv field cap, crashing the phase-2 DictReader with
# "_csv.Error: field larger than field limit". Raise the limit to a safe C-int max.
csv.field_size_limit(2**31 - 1)

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
        since_iso, until_iso, watermark = self._compute_window(resource, state)
        record_iter = self._record_source(resource, since_iso, until_iso)
        row_count, columns = self._stream_and_write_table(resource, record_iter)
        if row_count == 0:
            logging.info("No rows returned for resource '%s'; skipping table write.", resource.name)
        if self._effective_incremental(resource):
            # Advance watermark even on empty result to prevent unbounded window growth.
            self.write_state_file({STATE_LAST_RUN: watermark})
        if row_count:
            logging.info("Extracted resource '%s': %s rows, %s columns.", resource.name, row_count, len(columns))

    def _effective_incremental(self, resource: ResourceDef) -> bool:
        """The one predicate governing watermark, fetch window, and manifest flag (see resources)."""
        return effective_incremental(resource, self._config.incremental, self._config.primary_key)

    def _compute_window(self, resource: ResourceDef, state: dict[str, Any]) -> tuple[str | None, str | None, str]:
        """Return (since_iso, until_iso, watermark_iso) — the third value is the next-run watermark."""
        # A symbolic period replaces the date window; skip window and watermark logic.
        if self._config.symbolic_period:
            return None, None, datetime.now(UTC).isoformat()
        date_field = resource.date_field
        if not date_field:
            return None, None, datetime.now(UTC).isoformat()
        return resolve_window(
            state,
            date_field,
            self._config.since,
            self._effective_incremental(resource),
            self._config.until,
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
                query=self._config.payroll_query,
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
            page_size=self._config.page_size,
            max_pages=self._config.max_pages,
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

            # Phase 2: sorted columns → deterministic manifest schema across incremental runs.
            # Effective PK = the user-supplied primary_key if set, else the resource registry default.
            columns = sorted(seen_columns)
            primary_key = effective_primary_key(resource, self._config.primary_key)
            schema = {
                col: ColumnDefinition(
                    data_types=BaseType(
                        dtype=(SupportedDataTypes.TIMESTAMP if col in _TIMESTAMP_FIELDS else SupportedDataTypes.STRING)
                    ),
                    # Primary-key columns must be non-nullable — Keboola Storage rejects a PK
                    # defined on a nullable column when the output table is created.
                    nullable=col not in primary_key,
                    primary_key=col in primary_key,
                )
                for col in columns
            }
            # Same predicate as the watermark and window logic: incremental append/upsert
            # only with a stable PK; a keyless resource with no user PK always full-REPLACEs.
            is_incremental = self._effective_incremental(resource)
            table = self.create_out_table_definition(
                f"{resource.name}.csv",
                primary_key=primary_key,
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

    @sync_action("list_columns")
    def list_columns(self) -> list[SelectElement]:
        """Populate the Primary Key dropdown from the resource's output-table columns in Storage.

        The output table exists only after the first extraction run (see the field help), so this
        button is meant to be pressed once a table has been produced. Reading Storage needs the
        Storage token forwarded to the component (forwardToken) — surfaced as environment_variables.
        """
        if not self._config.resource:
            raise UserException("Select a resource first, then re-load columns.")
        env = self.environment_variables
        if not env.token or not env.url:
            raise UserException(
                "Storage token is not available. Enable token forwarding for this component so the "
                "column picker can read the output table."
            )
        if not env.component_id or not env.config_id:
            raise UserException("Component/configuration id is unavailable; cannot locate the output table.")
        resource = get_resource(self._config.resource)
        table_id = default_output_table_id(env.component_id, env.config_id, resource.name)
        try:
            columns = get_table_columns(env.url, env.token, table_id)
        except Exception as exc:
            raise UserException(f"Failed to read columns for table '{table_id}': {exc}") from exc
        if columns is None:
            raise UserException(
                f"No output table found at '{table_id}'. If you have not run this extraction yet, run it "
                "once to create the table, then re-load the columns. The picker resolves the config's "
                "default output bucket, so a dev branch or a custom output-bucket mapping is not supported "
                "— set the primary key manually in that case."
            )
        return [SelectElement(value=col, label=col) for col in columns]


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        # User-actionable error (bad config, API 4xx, unknown resource): log the message only.
        # A full traceback here is noise for the user and would leak local source paths — reserve
        # tracebacks for genuinely unexpected failures below (exit 2).
        logging.error(exc)
        exit(1)
    except Exception:
        logging.exception("Unexpected error")
        exit(2)
