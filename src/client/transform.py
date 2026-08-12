import json
from collections.abc import Iterator
from typing import Any


def flatten_record(record: dict[str, Any], parent: str = "", sep: str = "_") -> dict[str, Any]:
    """Flatten one nested object; serialize list/scalar-incompatible values to JSON strings."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        new_key = f"{parent}{sep}{key}" if parent else key
        if isinstance(value, dict):
            out.update(flatten_record(value, new_key, sep))
        elif isinstance(value, list):
            out[new_key] = json.dumps(value)
        else:
            out[new_key] = value
    return out


def explode_record(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Expand one metric entry into one row per line item of its single list section.

    A timecard_metrics entry (single metric select) is {"employeeId": {...}, "<section>": [items]}.
    Yield one dict per item, merged with the entry's non-list fields (employeeId) so every row is
    self-contained; callers still apply flatten_record. An entry with no list section yields the
    single entry unchanged (fallback); an empty section yields no rows. Only the FIRST list section
    is exploded — with a single metric select there is exactly one; any other list stays on the base
    dict for flatten_record to JSON-serialize.
    """
    list_keys = [key for key, value in record.items() if isinstance(value, list)]
    if not list_keys:
        yield record
        return
    section = list_keys[0]
    base = {key: value for key, value in record.items() if key != section}
    for item in record[section]:
        yield {**base, **item} if isinstance(item, dict) else {**base, section: item}
