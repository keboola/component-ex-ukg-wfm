# ex-ukg-wfm — UKG Pro Workforce Management Extractor — Design Spec

**Date:** 2026-07-15
**Component:** `keboola.ex-ukg-wfm` (vendor: `keboola`)
**Type:** Extractor (row-based)
**Status:** Draft for review

> Sibling to `component-ex-ukg-hr` but a **separate component** — different UKG product
> (Pro WFM / former Kronos Dimensions), different auth, different hosts, different data model.
> This spec targets **full scope** of the WFM read surface.

---

## 1. Source system

**UKG Pro Workforce Management** (formerly Workforce Dimensions / Kronos). REST API documented at
`developer.ukg.com/wfm`. Not related to UKG HR Service Delivery / PeopleDoc beyond the vendor name.

- **Host:** tenant vanity URL `https://<tenant>.<datacenter>.mykronos.com` (datacenter e.g. `prd`).
  Datacenter/region is assigned at provisioning and varies per customer — **taken as a config field,
  never composed by the component.**
- **API prefix:** `https://<HOST>/api/v1/...`. Auth lives outside the version prefix at
  `https://<HOST>/api/authentication/...`.
- **Path shape:** `/v1/<domain>/<resource>[/<operation>]`, e.g. `/v1/commons/data/multi_read`,
  `/v1/timekeeping/timecard_metrics/multi_read`.

## 2. Authentication & provisioning

- **Grant:** OAuth 2.0 **`password`** (resource-owner). No `client_credentials` — auth is **always as a
  user**, so a dedicated **service account** is required.
- **Token endpoint:** `POST /api/authentication/access_token`, `Content-Type:
  application/x-www-form-urlencoded`, body: `username`, `password`, `client_id`, `client_secret`,
  `grant_type=password`, `auth_chain=OAuthLdapService`.
- **Refresh:** same endpoint, `grant_type=refresh_token`. Refresh token lives ~7 days (8h for
  federated). Access token `expires_in` is returned but the exact value is not published → refresh
  proactively using `expires_in` with a safety margin (mirror HR client's `_TOKEN_SAFETY_MARGIN_S`).
- **Data calls:** `Authorization: Bearer <access_token>`.
- **`appkey`:** legacy — no longer required, ignored if sent. **Not** a config field.
- **Credential set (4 secrets + host):** `host`, `#client_id`, `#client_secret`, `#username`,
  `#password`.
- **Provisioning reality (open item):** `client_id`/`client_secret` are minted by UKG at tenant
  provisioning (support case / Salesforce), and a tenant "Developer Admin" must create the service
  account. **There is no public sandbox.** Consequence: no live auth check, no real VCR fixtures, and
  no cf-dev smoke test until the customer supplies a sandbox tenant. Build proceeds on documented
  behavior + synthetic fixtures; live verification is deferred and tracked as a blocker.

## 3. Capability inventory & scope — FULL SCOPE

Backbone (always built; everything else is driven off employee/org sets):

| Backbone | Endpoint style | Role |
|---|---|---|
| People / Persons | `POST /v1/commons/persons/...` (≤100 emp/call) | person + employment data |
| Business Structure | `GET`/`POST` (≤5,000 jobs/req) | org map, locations, jobs |
| Hyperfind | `POST` execute query | resolves employee-ID sets that drive multi_reads |
| Information Access (`/commons/data/multi_read`) | `POST` (≤500 emp, ≤365 days) | generic select-based metric/attribute engine |

Transactional families (all **In-scope**):

| Family | Key resources | Style / limits | Incremental |
|---|---|---|---|
| Timekeeping | punches, timecards, timecard_metrics | `POST multi_read` | date window (+ lookback for retro edits) |
| Scheduling | schedules, shifts, open shifts, swaps | `POST multi_read` | date window / symbolic period |
| Accruals | balances, transactions, summaries | `POST multi_read` | date / as-of |
| Leave | leave cases, edits, requests | `POST` (emp×days ≤84,000) | date window |
| Attendance | records, patterns, events | `POST` | date window |
| Attestations | attestation processes | `POST` | date window |
| Work / Activities | activities, activity_shifts (+ `net_changes` delta, ≤50 emp) | `POST multi_read` | **net_change delta** where available, else date window |
| Payroll | export / bulk download | `POST` async **submit → poll → download**, polled in-component | date window |
| Forecasting | labor/volume forecasts | `POST` | date window |

**No capability from research is excluded.** Any future cut is an explicit, recorded user decision.

## 4. Keboola mapping

- **Config rows, one per resource** (mirrors HR). Global config = auth (host + 4 secrets). Row =
  resource + window/symbolic-period + employee scope (Hyperfind ref or "all home") + select fields +
  load type.
- **Secrets:** `#`-prefixed → `KBC::ProjectSecure` encryption.
- **Incremental:** per-row `state.json` stores last window end; next window `[last_end - overlap, now]`,
  **split into ≤365-day sub-windows** to respect service limits. Net-change resources use their delta
  token. Advance watermark even on empty result (HR pattern).
- **Output:** one table per resource, `<resource>.csv`, sorted deterministic columns, native-type
  manifest (timestamps typed, rest string), incremental load with PK where the resource has a stable
  key. Reuse HR's disk-backed streaming writer verbatim.
- **Sync action:** `testConnection` mints a token.
- **Exit codes:** `UserException` → 1; unexpected → 2 (HR `__main__` pattern).

## 5. Code architecture (reuse vs new)

Reused from HR near-verbatim: `component.py` orchestration shell, `window.py` state/window,
streaming table writer, `transform.flatten_record`, `Configuration` (Pydantic) skeleton, backoff
helper shape.

New / rewritten:
- **`client/wfm_client.py`** — password-grant token + refresh; `Bearer` data calls; backoff on 429
  with **≥1s default** (no reliable `Retry-After`); on **413** reduce chunk size and retry.
- **`client/resources.py`** — richer `ResourceDef`: HTTP method, endpoint path, POST body template +
  `select` elements, batch limit (emp/call), pagination style (`cacheKey`+`count`/`index` vs none),
  incremental style (`date_window` | `symbolic_period` | `net_change`), primary key.
- **`client/orchestration`** — the multi_read engine: Hyperfind → chunk employee IDs by per-resource
  limit → for each chunk × date sub-window, POST multi_read → page via `cacheKey`/`count`/`index` →
  yield rows. Adaptive chunk shrink on 413.
- **async bulk engine (Payroll)** — submit export → poll job status on an interval (bounded by a
  max-wait ceiling; surface a `UserException` if the ceiling is hit rather than hanging) → download
  and stream the result. Polled in-component, consistent with our other async-export components.
- **`configuration.py`** — WFM fields (host, 4 secrets, resource, window, symbolic period, hyperfind,
  select, load type).

`run()` stays a thin orchestrator; logic in private methods (architecture checklist).

## 6. Testing

- **datadir tests** for each resource (config + expected output) using synthetic fixtures.
- **VCR functional tests** with **synthetic cassettes** (no sandbox) — hand-authored response bodies
  matching documented shapes; swap for real recordings once a sandbox tenant exists.
- Unit tests for: token/refresh logic, chunking + 413 shrink, date-window splitting (≤365d),
  cacheKey pagination.

## 7. Deployment

- Scaffold via `component-get-started`; register in Dev Portal under **`keboola`** vendor; bootstrap
  `0.0.1` release; CI property-sync from `component_config/`.
- Build `initial-implementation` branch image; **cf-dev smoke test deferred** until customer creds.

## 8. Risks / open items

1. **No public sandbox** → synthetic fixtures; live auth + smoke test blocked on customer tenant.
2. **Token lifetime unpublished** → refresh proactively off `expires_in` + margin.
3. **Service limits are tenant/contract specific** → design adaptive (413-driven) chunking, don't
   hardcode assumptions beyond documented caps.
4. **`Retry-After` often absent** → own backoff (≥1s default, exponential).
5. **Payroll async bulk** uses submit/poll/download — **polled in-component** (standard pattern) with a
   bounded max-wait. Revisit only if a tenant's export routinely runs for hours, which would exceed a
   reasonable in-component poll and argue for a different approach.
