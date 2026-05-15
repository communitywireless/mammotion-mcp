"""Area-name → HA switch entity resolver.

Reads ``data/area-mapping.json`` (baked into the container image at
``/app/data/area-mapping.json``) and resolves Joshua's app-side area names
("Area 6") to the canonical HA switch entity IDs that
``mammotion.start_mow`` expects in its ``areas`` list.

This module exists because HA's auto-generated friendly_names DO NOT match
the names Joshua sees in the Mammotion app. Agents that hard-coded entity
IDs from the HA dashboard got the wrong areas. The mapping JSON is the
single source of truth.

See ``rules/no-forward-todos.md``: drift detection is captured in the
mapping JSON's ``drift_detection`` block + tracked by mower-recovery-pm.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

LOGGER = logging.getLogger("mammotion_mcp.area_resolver")


@lru_cache(maxsize=4)
def _load_mapping(mapping_path: str) -> dict:
    """Load area-mapping.json from disk. Cached by path.

    Args:
        mapping_path: Absolute path to the area-mapping.json file.

    Returns:
        Parsed JSON dict.

    Raises:
        FileNotFoundError: if the mapping file is missing.
        json.JSONDecodeError: if the file is corrupt.
    """
    path = Path(mapping_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Area mapping not found at {mapping_path}. "
            f"Ensure the file is mounted into the container."
        )
    with path.open() as f:
        return json.load(f)


def resolve(area_name: str, mapping_path: str) -> str:
    """Return the HA switch entity ID for the named area.

    Args:
        area_name: Joshua-app-side area name (e.g. "Area 6").
        mapping_path: Absolute path to area-mapping.json.

    Returns:
        HA switch entity ID (e.g. "switch.luba2_awd_1_area_3439157731089703234").

    Raises:
        ValueError: if ``area_name`` not found in mapping. The error message
                    includes the list of valid names for caller correction.
    """
    data = _load_mapping(mapping_path)
    by_app = data.get("by_app_name", {})
    if area_name not in by_app:
        valid = ", ".join(sorted(by_app.keys()))
        raise ValueError(
            f"Unknown area: {area_name!r}. Valid names: {valid}"
        )
    entry = by_app[area_name]
    switch_entity = entry.get("ha_switch_entity")
    if not switch_entity:
        raise ValueError(
            f"Mapping entry for {area_name!r} missing ha_switch_entity. "
            f"area-mapping.json is corrupt."
        )
    return switch_entity


def list_areas(mapping_path: str) -> list[dict[str, str]]:
    """Return all areas as a list of dicts.

    Args:
        mapping_path: Absolute path to area-mapping.json.

    Returns:
        List of ``{app_name, hash, ha_switch_entity}`` dicts, one per area.
        Returns empty list if mapping file is unavailable (logged at ERROR).
    """
    try:
        data = _load_mapping(mapping_path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        LOGGER.error("Failed to load area mapping from %s: %s", mapping_path, exc)
        return []

    by_app = data.get("by_app_name", {})
    return [
        {
            "app_name": name,
            "hash": str(entry.get("hash", "")),
            "ha_switch_entity": entry.get("ha_switch_entity", ""),
        }
        for name, entry in by_app.items()
    ]


def clear_cache() -> None:
    """Clear the mapping cache. Useful for tests that mutate the file."""
    _load_mapping.cache_clear()
