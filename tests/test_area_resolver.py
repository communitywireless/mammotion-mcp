"""Tests for the area resolver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mammotion_mcp import area_resolver

DATA_PATH = Path(__file__).parent.parent / "data" / "area-mapping.json"


def test_area_mapping_has_area_6() -> None:
    """Area 6 is the canonical mower-living area and must always be present."""
    with open(DATA_PATH) as f:
        data = json.load(f)
    by_app = data.get("by_app_name", {})
    assert "Area 6" in by_app, "Area 6 must be in area-mapping.json"
    entry = by_app["Area 6"]
    assert entry["hash"] == "3439157731089703234"
    assert entry["ha_switch_entity"] == "switch.luba2_awd_1_area_3439157731089703234"


def test_all_eight_areas_present() -> None:
    """The farm has 8 areas; all must be in the mapping."""
    with open(DATA_PATH) as f:
        data = json.load(f)
    by_app = data.get("by_app_name", {})
    for n in range(1, 9):
        assert f"Area {n}" in by_app, f"Area {n} missing from area-mapping.json"


def test_resolve_area_6() -> None:
    """Resolver returns canonical switch entity for Area 6."""
    area_resolver.clear_cache()
    result = area_resolver.resolve("Area 6", str(DATA_PATH))
    assert result == "switch.luba2_awd_1_area_3439157731089703234"


def test_resolve_unknown_area_raises_with_valid_names() -> None:
    """Unknown area → ValueError listing valid names."""
    area_resolver.clear_cache()
    with pytest.raises(ValueError) as excinfo:
        area_resolver.resolve("Unknown Area", str(DATA_PATH))
    msg = str(excinfo.value)
    assert "Unknown Area" in msg
    assert "Area 6" in msg  # valid names listed


def test_list_areas_returns_eight() -> None:
    """list_areas returns all eight."""
    area_resolver.clear_cache()
    areas = area_resolver.list_areas(str(DATA_PATH))
    assert len(areas) == 8
    names = [a["app_name"] for a in areas]
    for n in range(1, 9):
        assert f"Area {n}" in names


def test_list_areas_returns_empty_on_missing_file(tmp_path) -> None:
    """list_areas returns empty list when mapping file is missing."""
    area_resolver.clear_cache()
    missing = tmp_path / "nope.json"
    assert area_resolver.list_areas(str(missing)) == []


def test_resolve_caches_mapping() -> None:
    """Mapping is cached by path — repeated calls do not re-read disk."""
    area_resolver.clear_cache()
    area_resolver.resolve("Area 6", str(DATA_PATH))
    cache_info_before = area_resolver._load_mapping.cache_info()  # noqa: SLF001
    area_resolver.resolve("Area 6", str(DATA_PATH))
    cache_info_after = area_resolver._load_mapping.cache_info()  # noqa: SLF001
    assert cache_info_after.hits > cache_info_before.hits
