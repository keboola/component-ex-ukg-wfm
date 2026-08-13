import csv
import logging
import tempfile
from collections.abc import Iterator
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
from client.window import resolve_window
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


def _alphabetized(elements: list[SelectElement]) -> list[SelectElement]:
    """Order dropdown options case-insensitively by their visible label.

    UI convention: a select the user scans (Hyperfinds, output columns, …) is sorted A→Z so an
    option is findable, rather than left in API/Storage response order.
    """
    return sorted(elements, key=lambda e: (e.label or "").casefold())


class Component(ComponentBase):
    # state.json key holding the sticky per-resource column set (see _load_sticky_columns).
    _STATE_COLUMNS_KEY = "schema_columns"

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
        # window_days chunks a date range into per-sub-window requests. That only makes sense for
        # per-event resources; for a period-rollup resource (timecard_metrics, accruals) it would
        # split the single per-employee aggregate row into partial-period rows, so refuse it here
        # with a clear pointer to the right memory lever. (Punch resources chunk by their own minute
        # cap, so window_days is simply ignored there — see iter_records.)
        if (
            self._config.window_days
            and resource.date_field
            and not resource.window_chunkable
            and resource.window_max_minutes == 0
        ):
            raise UserException(
                f"'window_days' is only supported for per-event resources; resource "
                f"'{resource.name}' cannot be split into date sub-windows (it is a period rollup, a "
                "net-change delta, or an org-level read, so it reads the whole range in one request). "
                "Remove Window Chunk Size (days) — for a rollup resource (timecard metrics, accruals) "
                "use 'batch_size' to control memory instead."
            )
        since_iso, until_iso = self._compute_window(resource)
        record_iter = self._record_source(resource, since_iso, until_iso)
        row_count, columns = self._stream_and_write_table(resource, record_iter)
        if row_count:
            logging.info("Extracted resource '%s': %s rows, %s columns.", resource.name, row_count, len(columns))

    def _effective_incremental(self, resource: ResourceDef) -> bool:
        """Predicate governing the Storage write mode (incremental upsert vs full replace) and the
        manifest `incremental` flag. The fetch window is independent — config-driven (see resources).
        """
        return effective_incremental(resource, self._config.incremental, self._config.primary_key)

    def _load_sticky_columns(self, resource: ResourceDef) -> list[str]:
        """Every column emitted for this resource on prior runs, persisted in state.json.

        Storage rejects a load whose column set is NARROWER than the destination table's — for an
        incremental upsert AND for a full REPLACE into a native-typed table. The API exposes no fixed
        schema (columns are data-derived), so a run whose data omits an optional field would shrink
        the set and fail the load. We remember the columns and re-emit their union, so the schema
        only ever grows (absent columns write empty). This state is per-row and unrelated to the
        fetch window (which stays config-driven).
        """
        bucket = (self.get_state_file() or {}).get(self._STATE_COLUMNS_KEY)
        cols = bucket.get(resource.name) if isinstance(bucket, dict) else None
        return [str(c) for c in cols] if isinstance(cols, list) else []

    def _extend_sticky_columns(self, resource: ResourceDef, seen_columns: list[str]) -> list[str]:
        """Union this run's columns with the persisted set, persist the grown set, and return it.

        Single state read + write (other state keys preserved). See _load_sticky_columns for why.
        """
        state = self.get_state_file() or {}
        bucket = state.get(self._STATE_COLUMNS_KEY)
        if not isinstance(bucket, dict):
            bucket = {}
        prior = bucket.get(resource.name)
        prior_cols = [str(c) for c in prior] if isinstance(prior, list) else []
        columns = sorted(set(seen_columns) | set(prior_cols))
        bucket[resource.name] = columns
        state[self._STATE_COLUMNS_KEY] = bucket
        self.write_state_file(state)
        return columns

    def _compute_window(self, resource: ResourceDef) -> tuple[str | None, str | None]:
        """Return (since_iso, until_iso) — the fetch window, driven purely by Start/End Date config.

        There is no state watermark: the window is recomputed from config every run. A symbolic
        period replaces the date window, and a resource with no date field has no window at all.

        A date-windowed resource needs BOTH bounds to build a valid WFM dateRange. `until` defaults
        to the run start when empty (resolve_window); `since` has no natural default. An empty Start
        Date is therefore rejected up front — otherwise the request would ship with no dateRange and
        WFM would silently return its default period (a near-empty result that then fails the output
        schema check). WFM's `dateRange.endDate` is INCLUSIVE and the API bounds are truncated to a
        calendar date, so a same-day window (Start == End) is a valid one-day pull; only a Start date
        strictly AFTER the End date's calendar day is rejected — that would yield an empty pull that
        reads as "the End Date parameter is broken".
        """
        if self._config.effective_symbolic_period or not resource.date_field:
            return None, None
        since_iso, until_iso = resolve_window(self._config.since, self._config.until)
        if since_iso is None:
            raise UserException(
                f"Resource '{resource.name}' needs a Start Date (the lower bound of the fetch "
                "window). Set a Start Date — optionally with an End Date to bound the pull — or "
                "switch Date Selection to a Symbolic Period."
            )
        # until_iso is always set (resolve_window defaults the upper bound to the run start).
        # Compare CALENDAR DATES, not full timestamps: a same-day window (Start == End) is valid
        # (the API truncates to a calendar date and endDate is inclusive), so only a Start date
        # whose calendar day is strictly after the End date's is an inverted window.
        if since_iso[:10] > until_iso[:10]:
            raise UserException(
                f"Start Date ({since_iso}) must be on or before End Date ({until_iso}) for resource "
                f"'{resource.name}'. Adjust the window so Start does not come after End."
            )
        return since_iso, until_iso

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
            select=self._config.effective_select,
            symbolic_period=self._config.effective_symbolic_period,
            page_size=self._config.page_size,
            max_pages=self._config.max_pages,
            hyperfind_threshold=self._config.hyperfind_threshold,
            batch_size=self._config.batch_size,
            window_days=self._config.window_days,
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
                return self._write_empty_table(resource)

            # Phase 2: sorted columns → deterministic manifest schema across incremental runs.
            # Effective PK = the user-supplied primary_key if set, else the resource registry default.
            primary_key = effective_primary_key(resource, self._config.primary_key)
            is_incremental = self._effective_incremental(resource)
            # Sticky schema: union this run's columns with every column seen before (state.json) so
            # the set never shrinks below the existing table, then persist the grown set. Columns
            # absent this run are written empty (restval=''). This applies to full loads too, not
            # just incremental: the output table is always native-typed, and Storage rejects a full
            # REPLACE whose column set is narrower than the destination's schema just as it rejects a
            # narrower incremental upsert (the "Missing columns: <field>" failure). A run that
            # legitimately returns few rows must not drop an optional column and break the table.
            columns = self._extend_sticky_columns(resource, list(seen_columns))
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
            # Storage write mode: incremental upsert only with a stable PK (computed above); a
            # keyless resource with no user PK always full-REPLACEs.
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

    def _write_empty_table(self, resource: ResourceDef) -> tuple[int, list[str]]:
        """Write a header-only output table when a run yields no rows.

        The header is the resource's known column set — its effective primary key plus every column
        seen on prior runs (sticky columns from state). When that set is non-empty the table is
        written empty, so a full load still replaces its destination (the Load Type help says full
        load "replaces it each run") and downstream configs keep a stable schema. Only when NOTHING
        is known — no primary key AND no sticky columns (e.g. a keyless resource whose first run
        returned nothing) — is there no schema to emit; there we leave the table unwritten and log
        that the destination's previous contents were kept (we cannot create a schema-less table).
        """
        primary_key = effective_primary_key(resource, self._config.primary_key)
        is_incremental = self._effective_incremental(resource)
        # Re-emit the full accumulated column set (from state) so a zero-row run still matches the
        # existing table's schema, with PK columns as the anchor. Applies to full loads too: a
        # native-typed table rejects a REPLACE with a narrower column set. A keyless resource with no
        # columns ever seen has no schema to emit, so we keep the destination's previous contents.
        sticky = self._load_sticky_columns(resource)
        header_cols = sorted(set(primary_key) | set(sticky))
        if not header_cols:
            logging.info(
                "No rows returned for resource '%s' and no primary key to build a header from; "
                "leaving the output table unwritten (a full load keeps its previous contents).",
                resource.name,
            )
            return 0, []
        schema = {
            col: ColumnDefinition(
                data_types=BaseType(
                    dtype=(SupportedDataTypes.TIMESTAMP if col in _TIMESTAMP_FIELDS else SupportedDataTypes.STRING)
                ),
                nullable=col not in primary_key,
                primary_key=col in primary_key,
            )
            for col in header_cols
        }
        table = self.create_out_table_definition(
            f"{resource.name}.csv",
            primary_key=primary_key,
            incremental=is_incremental,
            has_header=True,
            schema=schema,
        )
        with open(table.full_path, "w", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=header_cols).writeheader()
        self.write_manifest(table)
        if not is_incremental:
            # A full load's header-only table REPLACES the destination's previous contents with
            # nothing — a transient empty API response (rather than a genuinely empty resource)
            # would silently truncate real data with no other signal. An incremental zero-row run
            # is a no-op append, not a truncation, so it does not warn.
            logging.warning(
                "No rows returned for resource '%s' on a full load; replacing the output table with "
                "an empty (header-only) one. This will truncate any previously loaded data — check "
                "for an unexpected empty API response if this is not expected.",
                resource.name,
            )
        logging.info(
            "No rows returned for resource '%s'; wrote a header-only table with columns %s.",
            resource.name,
            ", ".join(header_cols),
        )
        return 0, header_cols

    @sync_action("testConnection")
    def test_connection(self) -> dict[str, str]:
        self.client.get_token()
        return {"status": "success"}

    @sync_action("list_hyperfinds")
    def list_hyperfinds(self) -> list[SelectElement]:
        """Populate the Hyperfind Query dropdown from the tenant's saved Hyperfind queries.

        GET /commons/hyperfind lists every query the account can see — public, personal and
        system ones (the latter carry negative ids, e.g. -9). The value is the numeric id the
        extraction sends as the employee scope; the label pairs the human name with that id so a
        user picks by name instead of guessing an id.
        """
        result = self.client.get_json("/commons/hyperfind")
        queries = result.get("hyperfindQueries", []) if isinstance(result, dict) else []
        elements: list[SelectElement] = []
        for query in queries:
            if not isinstance(query, dict) or "id" not in query:
                continue
            qid = query["id"]
            name = query.get("name") or query.get("qualifier") or str(qid)
            elements.append(SelectElement(value=str(qid), label=f"{name} ({qid})"))
        return _alphabetized(elements)

    @sync_action("list_symbolic_periods")
    def list_symbolic_periods(self) -> list[SelectElement]:
        """Populate the Symbolic Period dropdown from the tenant's symbolic periods.

        GET /commons/symbolicperiod returns [{id, symbolicId, name, periodTypeId, sortOrder}]. The
        id is tenant-specific and the only form WFM accepts (a qualifier string is rejected), so the
        value is the numeric id; the label pairs the name with its period type (e.g.
        "Current Pay Period [TIMEKEEPING]") because names repeat across types (a metrics/timekeeping
        read wants a TIMEKEEPING period, a scheduling read a SCHEDULING one).
        """
        result = self.client.get_json("/commons/symbolicperiod")
        periods = result if isinstance(result, list) else []

        def _name(p: dict[str, Any]) -> str:
            return str(p.get("name") or p.get("symbolicId") or p.get("id"))

        # Group by period type (names repeat across types), then A→Z by name so the list is
        # scannable; sortOrder only breaks exact-name ties, keeping the order deterministic.
        elements: list[SelectElement] = []
        for period in sorted(
            (p for p in periods if isinstance(p, dict) and "id" in p),
            key=lambda p: (str(p.get("periodTypeId") or ""), _name(p).casefold(), p.get("sortOrder") or 0),
        ):
            pid = period["id"]
            ptype = period.get("periodTypeId")
            label = f"{_name(period)} [{ptype}]" if ptype else _name(period)
            elements.append(SelectElement(value=str(pid), label=label))
        return elements

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
            raise UserException(
                "Column auto-loading isn't available until this configuration is saved and has run once. "
                "For now, type the primary-key column name(s) directly into the field."
            )
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
        return _alphabetized([SelectElement(value=col, label=col) for col in columns])


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
