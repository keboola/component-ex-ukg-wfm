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
| `since` | Lower bound (ISO 8601 or a relative phrase like `30 days ago`). For incremental resources with a stable primary key it seeds the first run, after which the state watermark supersedes it; resources without a stable key (and any `full_load`) apply `since` on every run. |
| `overlap_margin_seconds` | Seconds subtracted from the watermark to re-capture late/retro-edited records. |
| `hyperfind_ref` | Hyperfind query id/name resolving the employee-ID set. Empty = tenant `All Home` default (employee-scoped resources only). |
| `select` | Optional list of API `select` elements; empty uses the resource default. |
| `symbolic_period` | Optional symbolic period (e.g. `Current Pay Period`) for period-based reads. |
| `date_field` | Advanced: override the datetime field used for the incremental window. |
| `max_wait_seconds` | Payroll export only: maximum polling time before failing (default 1800). |
| `poll_interval_seconds` | Payroll export only: seconds between status polls (default 15). |
