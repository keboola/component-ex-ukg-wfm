import pytest
import requests
import requests_mock

from client.storage import default_output_bucket_id, default_output_table_id, get_table_columns

STACK = "https://connection.keboola.com"


def test_default_bucket_id_sanitizes_component_dots():
    # Keboola default bucket: stage `in`, name c-{componentId dots->dashes}-{configId}.
    assert default_output_bucket_id("keboola.ukg-wfm", "12345") == "in.c-keboola-ukg-wfm-12345"


def test_default_table_id_appends_table_name():
    assert default_output_table_id("keboola.ukg-wfm", "12345", "persons") == "in.c-keboola-ukg-wfm-12345.persons"


def test_get_table_columns_returns_column_list():
    table_id = "in.c-keboola-ukg-wfm-1.persons"
    with requests_mock.Mocker() as m:
        m.get(
            f"{STACK}/v2/storage/tables/{table_id}",
            json={"id": table_id, "columns": ["personNumber", "firstName", "lastName"]},
        )
        assert get_table_columns(STACK, "tok", table_id) == ["personNumber", "firstName", "lastName"]
        assert m.last_request.headers["X-StorageApi-Token"] == "tok"


def test_get_table_columns_returns_none_on_404():
    # Table not created yet (before the first pull) -> None, so the sync action can tell the user
    # to run once rather than surfacing a raw HTTP error.
    table_id = "in.c-keboola-ukg-wfm-1.persons"
    with requests_mock.Mocker() as m:
        m.get(f"{STACK}/v2/storage/tables/{table_id}", status_code=404, json={"error": "not found"})
        assert get_table_columns(STACK, "tok", table_id) is None


def test_get_table_columns_raises_on_other_http_error():
    table_id = "in.c-keboola-ukg-wfm-1.persons"
    with requests_mock.Mocker() as m:
        m.get(f"{STACK}/v2/storage/tables/{table_id}", status_code=403, json={"error": "forbidden"})
        with pytest.raises(requests.HTTPError):
            get_table_columns(STACK, "tok", table_id)
