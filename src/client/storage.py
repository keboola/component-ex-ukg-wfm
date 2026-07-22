"""Minimal Keboola Storage API reader for sync actions.

The only thing the UI needs from Storage is the column list of an already-extracted output table,
used to populate the Primary Key picker. Sync actions run with the Storage token forwarded as
KBC_TOKEN (exposed via ComponentBase.environment_variables.token) and the stack endpoint as KBC_URL,
so a single authenticated GET on the table-detail endpoint is enough — no kbcstorage dependency.
"""

import requests

_TIMEOUT_SECONDS = 30


def default_output_bucket_id(component_id: str, config_id: str) -> str:
    """Keboola's default output bucket id for a component config.

    Stage ``in``, bucket name ``c-{componentId}-{configId}`` with the componentId's dots replaced by
    dashes (e.g. ``keboola.ukg-wfm`` -> ``keboola-ukg-wfm``). This component sets ``defaultBucket``,
    so every row's output table lands here. (A config with a custom output-bucket mapping would not
    match; the picker then reports the table as not found, which is the correct signal.)
    """
    sanitized = component_id.replace(".", "-")
    return f"in.c-{sanitized}-{config_id}"


def default_output_table_id(component_id: str, config_id: str, table_name: str) -> str:
    """Storage table id of a resource's output table in the config's default bucket."""
    return f"{default_output_bucket_id(component_id, config_id)}.{table_name}"


def get_table_columns(base_url: str, token: str, table_id: str) -> list[str] | None:
    """Return a Storage table's column names, or None if the table does not exist yet (HTTP 404)."""
    url = f"{base_url.rstrip('/')}/v2/storage/tables/{table_id}"
    response = requests.get(url, headers={"X-StorageApi-Token": token}, timeout=_TIMEOUT_SECONDS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json().get("columns", [])
