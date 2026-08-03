## Global configuration (authentication)

| Field | Secret | Description |
|---|---|---|
| `host` | no | Your UKG tenant sign-in URL, host only, e.g. `https://mycompany.prd.mykronos.com`. |
| `#client_id` | yes | OAuth client id issued by UKG when your tenant is set up. |
| `#client_secret` | yes | OAuth client secret issued by UKG when your tenant is set up. |
| `#username` | yes | Username of the dedicated UKG service account this extractor signs in as. |
| `#password` | yes | Password of that UKG service account. |

Use the **Test Connection** button to verify your credentials.

## Row configuration (one per resource)

| Field | Description |
|---|---|
| `resource` | The UKG WFM resource to extract (see the resource dropdown / README table). |
| `load_type` | `full_load` replaces the table each run; `incremental_load` upserts by primary key. Resources without a stable primary key always run full-load. |
| `since` | Start Date — lower bound of the fetch window (ISO 8601 or a relative phrase like `30 days ago`). Applied on every run; Load Type changes only how rows are written, not what is fetched. Ignored when a symbolic period is set. |
| `until` | End Date — optional upper bound (ISO 8601 or a relative phrase). Empty = up to the current run time. Ignored when a symbolic period is set. |
| `hyperfind_ref` | Hyperfind query resolving the employee set. Empty = the tenant `All Home` default (employee-scoped resources only). |
| `select` | Optional list of API `select` fields; empty uses the resource default. |
| `symbolic_period` | Optional symbolic-period **id** (e.g. `1` = Current Pay Period) used instead of an explicit Start/End window. Pick it with the Load Symbolic Periods button; only the id works, not a name. When set, it takes precedence and Start/End Date are ignored. Not supported for scheduling resources. |
| `primary_key` | Incremental load only. Column(s) that identify a row for upsert. Pick from the output-table columns (Load Columns from Storage, available after the first run) or type them directly. Overrides the resource default and can enable upsert for an otherwise keyless resource. |
| `max_wait_seconds` | Payroll export only: maximum polling time before failing (default 1800). |
| `poll_interval_seconds` | Payroll export only: seconds between status polls (default 15). |
