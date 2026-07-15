# VCR cassettes — SYNTHETIC

These cassettes are **hand-authored** to documented UKG Pro WFM API response shapes.
There is **no public sandbox** for this component and no real credentials, so no real
interactions were recorded. Each cassette is replayed with `record_mode="none"`, so the
network is never touched — if the component issued a request not present in a cassette,
VCR would raise instead of recording it, which keeps the cassettes honest about the flow.

| Cassette | Flow exercised |
| --- | --- |
| `timekeeping_punches.yaml` | date-window resource: token → hyperfind → single-page multi_read |
| `timekeeping_punches_paged.yaml` | multi_read `cacheKey`/`index` paging (page 1 full → page 2 partial → stop) |
| `payroll_export_async.yaml` | async export: submit → poll (`IN_PROGRESS` → `COMPLETED`) → download file |
| `scheduling_shifts_symbolic.yaml` | `symbolic_period` bound: token → hyperfind → multi_read |
| `auth_failure.yaml` | token endpoint `401` → `UserException` (entrypoint maps to exit 1) |

Replace these with real `record_mode="once"` recordings once a customer sandbox tenant
with a provisioned service account and minted `client_id`/`client_secret` is available.
Secret values (password, client_secret, username, client_id) and the Authorization header
are filtered out by the VCR config in `../test_functional_vcr.py`.
