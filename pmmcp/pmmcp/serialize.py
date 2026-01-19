"""Generic serialization for SDK return types."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


def serialize(obj: Any) -> Any:
    """Recursively convert dataclasses/objects to JSON-serializable dicts.

    Handles:
    - Primitives (str, int, float, bool, None)
    - Lists and tuples
    - Dicts
    - Dataclasses (via asdict)
    - Decimal (to float)
    - datetime (to ISO string)
    - Objects with __dict__ (fallback)

    Args:
        obj: Any object to serialize

    Returns:
        JSON-serializable representation
    """
    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, datetime):
        return obj.isoformat()

    if isinstance(obj, (list, tuple)):
        return [serialize(item) for item in obj]

    if isinstance(obj, dict):
        return {str(k): serialize(v) for k, v in obj.items()}

    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: serialize(v) for k, v in asdict(obj).items()}

    # Fallback: try __dict__ for objects
    if hasattr(obj, "__dict__"):
        return {
            k: serialize(v)
            for k, v in obj.__dict__.items()
            if not k.startswith("_")
        }

    # Last resort: string representation
    return str(obj)
