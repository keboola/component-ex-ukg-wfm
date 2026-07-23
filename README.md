ex-ukg-wfm
=============

Keboola extractor for **UKG Pro Workforce Management** (formerly Workforce Dimensions / Kronos).
Reads the full WFM API surface behind an OAuth password-grant service account, driven by a
data-driven resource registry, a Hyperfind + chunking `multi_read` engine, and an async
submit/poll/download engine for Payroll.

**Table of Contents:**

[TOC]

Overview
========

One configuration row = one resource. The global configuration holds authentication; each row
selects a resource plus load type, date window / symbolic period, Hyperfind employee scope, and
select fields. Rows stream to one output table per resource with a native-type manifest.

Authentication & provisioning
==============================

- **Grant:** OAuth 2.0 `password` (resource-owner). Auth is always as a user, so a dedicated
  **service account** is required. No `client_credentials`.
- **Host:** the tenant vanity URL (e.g. `https://acme.prd.mykronos.com`) is a configuration
  field, never composed. Data calls hit `https://<HOST>/api/v1/...`; auth hits
  `https://<HOST>/api/authentication/access_token`.
- **Credentials:** `host` + four secrets (`#client_id`, `#client_secret`, `#username`,
  `#password`). `client_id`/`client_secret` are minted by UKG at tenant provisioning; a tenant
  Developer Admin must create the service account. The legacy `appkey` is not used.
- **Tokens:** access token lifetime is unpublished, so the client refreshes proactively from
  `expires_in` minus a safety margin, using `grant_type=refresh_token` when available.
- **No public sandbox:** there is no self-service WFM sandbox, so credentials come from a
  provisioned customer/partner tenant. The contract has been validated against a live tenant (see
  Known limitations for the two license-gated families).

Supported resources
===================

| Family | Resources |
|---|---|
| People | `persons` |
| Business Structure | `business_structure` (legacy `/commons/locations` org-map read) |
| Hyperfind | `hyperfind_queries` |
| Timekeeping | `timekeeping_punches`, `timekeeping_timecards`, `timekeeping_timecard_metrics` |
| Scheduling | `scheduling_schedules`, `scheduling_shifts`, `scheduling_open_shifts`, `scheduling_swaps` |
| Accruals | `accruals_balances`, `accruals_transactions`, `accruals_summaries` (all via `timecard_metrics` `select`) |
| Leave | `leave_cases`, `leave_edits` |
| Attendance | `attendance_records`, `attendance_events` |
| Attestations | `attestations` |
| Work / Activities | `work_activities`, `work_activity_shifts`, `work_activity_net_changes` |
| Payroll | `payroll_export` (async submit → poll → download) |
| Forecasting | `forecasting` |

> The three **Accruals** resources share `POST /timekeeping/timecard_metrics/multi_read`, differing only
> by the `select` value: `accruals_balances` and `accruals_summaries` use `ACCRUAL_SUMMARY` (which carries
> balance data), `accruals_transactions` uses `ACCRUAL_TRANSACTIONS`. There is no bulk `/accruals/*` read.
>
> The **Work / Activities** resources require the Activities Integration API license, which is off on the
> reference tenant (HTTP 403 `WFA-000030`); their request shapes are verified but a live 200 is gated on
> licensing. **Forecasting** needs a non-empty tenant-configured `categoryDrivers` set; the reference
> tenant has none configured, so it cannot yet return data (`WFF-270000`).

Incremental & windowing
=======================

- Incremental resources store the last window end in per-row `state.json`; the next run reads
  `[last_end, now]`.
- **Start Date (`since`)** bounds the API fetch and is independent of the load type. It seeds the
  first run of a primary-key resource on incremental load (state watermark takes over afterwards),
  and is applied on every run for full load and for keyless resources.
- **End Date (`until`)** optionally bounds the upper end of the fetch window (empty = the current
  run time); when set it also becomes the persisted watermark so the next run continues from there.
- The window is split into **≤365-day sub-windows** to respect service limits; employee sets are
  chunked by each resource's per-call batch limit (≤500 for most, 100 for persons, 50 for activity
  net-changes) and adaptively **halved on HTTP 413**.
- Net-change resources currently run as a date-windowed full **replace** (like other keyless
  resources); true net-change delta is deferred until a resource gains a stable primary key and
  persisted change-token.
- For effectively-incremental resources the watermark advances **even on an empty result** to prevent
  unbounded window growth.
- **Incremental requires a primary key.** A resource upserts incrementally only when a primary key
  exists — either its registry default or one supplied via the row's `primary_key` field. A
  registry-keyless resource with no user-supplied `primary_key` always runs full-replace
  (incremental-without-PK would append unboundedly). The `primary_key` UI field is a Storage-backed
  column picker: the `list_columns` sync action reads the resource's output table from Storage, so it
  is populated only after the first run has created that table (requires the component's
  `forwardToken` flag, set once in the Developer Portal by an admin).
- A **symbolic period** replaces the date window entirely — when set, Start Date / End Date are
  ignored, so configure one or the other, not both.

Payroll async export
====================

`payroll_export` submits an export job, polls its status on an interval bounded by
`max_wait_seconds` (raising a user error rather than hanging), then downloads and streams the
result. The download and all scratch files stage in `/tmp`, never under `data/out/tables/`.

Testing
=======

- Unit tests: token/refresh, chunking + 413 shrink, window splitting, apply_read count/index
  paging, payroll poll ceiling.
- datadir mock tests (`tests/mock/`): one synthetic fixture per family plus an auth-failure case.
- VCR functional tests (`tests/functional/`): recorded against a real tenant and replayed
  deterministically in CI (no network, no credentials). Cassettes are **sanitized** — the tenant
  host is rewritten to a placeholder and employee PII / identifying fields are redacted — and
  volume-capped (≤25 records/array). Re-record with `component-developer:generate-vcr-tests`.

Re-record in a single step (needs `secrets.json` with real tenant credentials):

~~~~
uv run python -m keboola.datadirtest scaffold --secrets secrets.json --regenerate
~~~~

`VCR_SANITIZERS` in `src/component.py` tags the PII redaction (`BodyFieldSanitizer`) and the array
cap with `scrub_before_read=True` (keboola.vcr ≥ 0.7.0), so the component reads already-scrubbed,
capped responses **at record time** — `expected/`, `logs.json`, and `output_snapshot.json` are
captured clean and match replay, no post-processing needed. Credentials, the OAuth token, and
numeric employee ids stay real during recording (they are round-tripped into later requests) and are
redacted only in the cassette. API-error logs record the endpoint path, not the full URL, so failure
tests don't diverge on the sanitized-vs-placeholder host. After re-recording, run the cassette
validation gate (no leaked secrets/host/PII; recordings match intent) before committing.

Run the suite and lint:

~~~~
uv run pytest -q
uv run ruff check src/ tests/
~~~~

Known limitations / blockers
============================

- The endpoint contract (paths, request bodies, envelopes, primary keys) is **verified against a
  live WFM tenant**. Two resource families could not return data on that tenant and are documented
  but unverified end-to-end: **Work/Activities** (`work_activities`, `work_activity_shifts`,
  `work_activity_net_changes`) require the Activities Integration API license (HTTP 403
  `WFA-000030`), and **forecasting** requires tenant-configured `categoryDrivers` (`WFF-270000`).
  Their request shapes are set from the reference docs; enable on a licensed/configured tenant.
- `timekeeping_punches` is a near-real-time reader: the API caps it at a 60-minute window per call
  (count ≤ 25), so a wide backfill fans out into many calls — prefer short, frequent incremental
  windows over large date ranges.
- `payroll_export` needs a tenant-specific SQL `payroll_query`; `forecasting` needs the tenant's
  volume-driver/category refs. Both are configuration inputs, not defaults.

Development
-----------

Clone, build, and run with docker-compose:

~~~~
git clone component-ex-ukg-wfm
cd component-ex-ukg-wfm
docker-compose build
docker-compose run --rm dev
docker-compose run --rm test
~~~~

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer documentation](https://developers.keboola.com/extend/component/deployment/).
