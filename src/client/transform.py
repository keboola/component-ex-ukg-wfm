import json
from typing import Any


def flatten_record(record: dict, parent: str = "", sep: str = "_") -> dict[str, Any]:
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
