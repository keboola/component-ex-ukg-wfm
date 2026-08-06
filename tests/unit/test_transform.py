from client.transform import flatten_record


def test_flatten_nested_and_lists():
    row = flatten_record({"id": 1, "person": {"name": "A"}, "tags": [1, 2]})
    assert row == {"id": 1, "person_name": "A", "tags": "[1, 2]"}
