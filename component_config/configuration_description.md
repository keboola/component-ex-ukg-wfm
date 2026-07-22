## Global configuration (authentication)

| Field | Secret | Description |
|---|---|---|
| `host` | no | Tenant vanity URL, e.g. `https://acme.prd.mykronos.com` (no trailing path). |
| `#client_id` | yes | OAuth client id minted by UKG at tenant provisioning. |
| `#client_secret` | yes | OAuth client secret minted by UKG at tenant provisioning. |
| `#username` | yes | Username of the dedicated WFM service account (password grant). |
| `#password` | yes | Password of the dedicated WFM service account. |

Use the **Test Connection** button to mint a token and verify credentials.

## Row configuration (one per resource)

| Field | Description |
|---|---|
| `resource` | The WFM resource to extract (see the resource dropdown / README table). |
| `load_type` | `full_load` or `incremental_load`. Resources without a stable primary key always run full-load. |
| `since` | Lower bound of the API fetch window (ISO 8601 or a relative phrase like `30 days ago`). Independent of load type: for a primary-key resource on incremental load it seeds the first run, after which the state watermark takes over; for full load (and any keyless resource) it is applied on every run. |
| `until` | Optional upper bound of the fetch window (ISO 8601 or a relative phrase). Empty = up to the current run time; set it to bound a backfill to a fixed window. |
| `hyperfind_ref` | Hyperfind query id/name resolving the employee-ID set. Empty = tenant `All Home` default (employee-scoped resources only). |
| `select` | Optional list of API `select` elements; empty uses the resource default. |
| `symbolic_period` | Optional UKG-named relative period (e.g. `Current Pay Period`) supplied instead of an explicit start/end date; applies to date-windowed resources only. |
| `primary_key` | Incremental load only. Column(s) that uniquely identify a row, used to upsert. Overrides the resource registry default and enables incremental upsert even for a registry-keyless resource. Empty uses the resource default. |
| `max_wait_seconds` | Payroll export only: maximum polling time before failing (default 1800). |
| `poll_interval_seconds` | Payroll export only: seconds between status polls (default 15). |
