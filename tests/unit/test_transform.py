from client.transform import explode_record, flatten_record


def test_explode_one_row_per_line_item():
    entry = {
        "employeeId": {"id": 14212},
        "actualTotals": [
            {"uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
            {"uniqueId": "14212:2026-07-26:801", "applyDate": "2026-07-26", "hoursAmount": 6.0},
        ],
    }
    rows = [flatten_record(r) for r in explode_record(entry)]
    assert rows == [
        {"employeeId_id": 14212, "uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
        {"employeeId_id": 14212, "uniqueId": "14212:2026-07-26:801", "applyDate": "2026-07-26", "hoursAmount": 6.0},
    ]


def test_explode_empty_section_yields_no_rows():
    assert list(explode_record({"employeeId": {"id": 67127}, "actualTotals": []})) == []


def test_explode_no_list_section_yields_no_rows():
    assert list(explode_record({"employeeId": {"id": 1}})) == []


def test_explode_scalar_list_items_wrap_under_section_key():
    entry = {"employeeId": {"id": 1}, "vals": [10, 20]}
    assert list(explode_record(entry)) == [
        {"employeeId": {"id": 1}, "vals": 10},
        {"employeeId": {"id": 1}, "vals": 20},
    ]


def test_flatten_nested_and_lists():
    row = flatten_record({"id": 1, "person": {"name": "A"}, "tags": [1, 2]})
    assert row == {"id": 1, "person_name": "A", "tags": "[1, 2]"}
