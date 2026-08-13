## Global configuration (authentication)

| Field | Secret | Description |
|---|---|---|
| `host` | no | Your UKG tenant sign-in URL, host only, e.g. `https://mycompany.prd.mykronos.com`. |
| `client_id` | no | OAuth client id issued by UKG when your tenant is set up. |
| `#client_secret` | yes | OAuth client secret issued by UKG when your tenant is set up. |
| `username` | no | Username of the dedicated UKG service account this extractor signs in as. |
| `#password` | yes | Password of that UKG service account. |

Use the **Test Connection** button to verify your credentials.

## Row configuration (one per resource)

| Field | Description |
|---|---|
| `resource` | The UKG WFM resource to extract (see the resource dropdown / README table). |
| `window_type` | Date Selection: `date_window` (explicit Start/End Date) or `symbolic_period` (a rolling UKG period). Chooses which of the two the form shows. |
| `since` | Start Date — lower bound of the fetch window (ISO 8601 or a relative phrase like `30 days ago`). **Required** for a date-windowed resource (a run with no Start Date and no Symbolic Period fails fast). Applied on every run; Load Type changes only how rows are written, not what is fetched. Shown when Date range is selected. |
| `until` | End Date — optional upper bound (ISO 8601 or a relative phrase). Empty = up to the current run time. Must be after Start Date. Shown when Date range is selected. |
| `window_days` | Optional. Max span in days of each date sub-window when chunking a Start/End pull on a **per-event** resource (schedules, shifts, timecards, leave, attendance, attestations) — lower it to fetch a large range in smaller pieces and keep memory under the limit. Empty = 365 days. **Not supported for rollup resources** (Timecard Metrics, Accruals), which return one total per employee — use a smaller employee `batch_size` there. Punch-level resources use their own per-call cap. Shown when Date range is selected. |
| `symbolic_period` | Symbolic-period **id** (e.g. `1` = Current Pay Period). Pick it with the Load Symbolic Periods button; only the id works, not a name. Shown when Symbolic period is selected. Not supported for scheduling resources. |
| `hyperfind_ref` | Hyperfind query resolving the employee set. Empty = the tenant `All Home` default (employee-scoped resources only). |
| `load_type` | `full_load` replaces the table each run; `incremental_load` upserts by primary key. Resources without a stable primary key always run full-load. |
| `primary_key` | Incremental load only. Column(s) that identify a row for upsert. The API returns no field list, so columns are read from Storage after the first run (Load Columns from Storage) — or type them directly. Overrides the resource default and can enable upsert for an otherwise keyless resource. |
| `max_wait_seconds` | Payroll export only: maximum polling time before failing (default 1800). |
| `poll_interval_seconds` | Payroll export only: seconds between status polls (default 15). |
