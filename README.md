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
- **No public sandbox** → live authentication and the cf-dev smoke test are **deferred** until a
  customer sandbox tenant is supplied (see Known limitations).

Supported resources
===================

| Family | Resources |
|---|---|
| People | `persons` |
| Business Structure | `business_structure` |
| Hyperfind | `hyperfind_queries` |
| Information Access | `information_access` |
| Timekeeping | `timekeeping_punches`, `timekeeping_timecards`, `timekeeping_timecard_metrics` |
| Scheduling | `scheduling_schedules`, `scheduling_shifts`, `scheduling_open_shifts`, `scheduling_swaps` |
| Accruals | `accruals_balances`, `accruals_transactions`, `accruals_summaries` |
| Leave | `leave_cases`, `leave_edits`, `leave_requests` |
| Attendance | `attendance_records`, `attendance_patterns`, `attendance_events` |
| Attestations | `attestations` |
| Work / Activities | `work_activities`, `work_activity_net_changes` |
| Payroll | `payroll_export` (async submit → poll → download) |
| Forecasting | `forecasting` |

Incremental & windowing
=======================

- Incremental resources store the last window end in per-row `state.json`; the next run reads
  `[last_end − overlap, now]`.
- The window is split into **≤365-day sub-windows** to respect service limits; employee sets are
  chunked by each resource's per-call batch limit (≤500 for most, 100 for persons, 50 for activity
  net-changes) and adaptively **halved on HTTP 413**.
- Net-change resources currently run as a date-windowed full **replace** (like other keyless
  resources); true net-change delta is deferred until a resource gains a stable primary key and
  persisted change-token.
- For effectively-incremental resources the watermark advances **even on an empty result** to prevent
  unbounded window growth.
- **Incremental requires a stable primary key.** Resources without one always run full-load
  (incremental-without-PK would append unboundedly).

Payroll async export
====================

`payroll_export` submits an export job, polls its status on an interval bounded by
`max_wait_seconds` (raising a user error rather than hanging), then downloads and streams the
result. The download and all scratch files stage in `/tmp`, never under `data/out/tables/`.

Testing
=======

- Unit tests: token/refresh, chunking + 413 shrink, ≤365-day window splitting, cacheKey
  pagination, payroll poll ceiling.
- datadir mock tests (`tests/mock/`): one synthetic fixture per family plus an auth-failure case.
- VCR functional tests are **deferred** — cassettes must be *recorded* from a real tenant, which
  requires credentials that do not exist yet (see blockers below). They are not hand-authored.

Run the suite and lint:

~~~~
uv run pytest -q
uv run ruff check src/ tests/
~~~~

Known limitations / blockers
============================

- **No public UKG WFM sandbox.** The live auth check, VCR recording (real cassettes), and the
  cf-dev smoke test are all blocked until a customer sandbox tenant with a provisioned service
  account and minted `client_id`/`client_secret` is available. Once supplied, record VCR cassettes
  with `component-developer:generate-vcr-tests` (record mode), then run `testConnection` and a
  `timekeeping_punches` job to verify. Local coverage today is unit + datadir mock tests only.
- Endpoint paths and primary keys reflect best-known documented WFM shapes; because the engine is
  data-driven, correcting any path/PK is a one-line registry edit in `src/client/resources.py`.

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
