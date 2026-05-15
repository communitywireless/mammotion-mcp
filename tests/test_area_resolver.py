"""Tests for the area resolver.

Covers three loading paths:
1. Package-resource path (mapping_path=None) — canonical for uvx-installed wheels.
2. Explicit-path override (mapping_path=<str>) — backward-compat for Docker/custom.
3. Missing-file graceful return (list_areas returns []).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mammotion_mcp import area_resolver

# Explicit path to the data file in its new package-relative location.
DATA_PATH = Path(__file__).parent.parent / "mammotion_mcp" / "data" / "area-mapping.json"

# Expected canonical values (frozen; change only if area-mapping.json changes).
_AREA_6_HASH = "3439157731089703234"
_AREA_6_ENTITY = "switch.luba2_awd_1_area_3439157731089703234"
_TOTAL_AREAS = 8


# ---------------------------------------------------------------------------
# Data-file sanity (direct JSON read, no resolver dependency)
# ---------------------------------------------------------------------------

def test_area_mapping_file_in_package_dir() -> None:
    """area-mapping.json must live inside mammotion_mcp/data/ (package data)."""
    assert DATA_PATH.exists(), (
        f"area-mapping.json not found at {DATA_PATH}. "
        "Did you forget to `git mv data/area-mapping.json mammotion_mcp/data/`?"
    )


def test_area_mapping_has_area_6() -> None:
    """Area 6 is the canonical mower-living area and must always be present."""
    with open(DATA_PATH) as f:
        data = json.load(f)
    by_app = data.get("by_app_name", {})
    assert "Area 6" in by_app, "Area 6 must be in area-mapping.json"
    entry = by_app["Area 6"]
    assert entry["hash"] == _AREA_6_HASH
    assert entry["ha_switch_entity"] == _AREA_6_ENTITY


def test_all_eight_areas_present() -> None:
    """The farm has 8 areas; all must be in the mapping."""
    with open(DATA_PATH) as f:
        data = json.load(f)
    by_app = data.get("by_app_name", {})
    for n in range(1, _TOTAL_AREAS + 1):
        assert f"Area {n}" in by_app, f"Area {n} missing from area-mapping.json"


# ---------------------------------------------------------------------------
# Package-resource path (mapping_path=None) — primary path for uvx installs
# ---------------------------------------------------------------------------

def test_resolve_area_6_package_default() -> None:
    """resolve() with None loads the bundled package default."""
    area_resolver.clear_cache()
    result = area_resolver.resolve("Area 6", None)
    assert result == _AREA_6_ENTITY


def test_list_areas_package_default() -> None:
    """list_areas(None) returns all eight areas from the bundled default."""
    area_resolver.clear_cache()
    areas = area_resolver.list_areas(None)
    assert len(areas) == _TOTAL_AREAS
    names = [a["app_name"] for a in areas]
    for n in range(1, _TOTAL_AREAS + 1):
        assert f"Area {n}" in names


def test_resolve_unknown_area_package_default_raises() -> None:
    """Unknown area with None path raises ValueError listing valid names."""
    area_resolver.clear_cache()
    with pytest.raises(ValueError) as excinfo:
        area_resolver.resolve("Unknown Area", None)
    msg = str(excinfo.value)
    assert "Unknown Area" in msg
    assert "Area 6" in msg  # valid names listed


# ---------------------------------------------------------------------------
# Explicit-path override — backward-compat for Docker / custom deployments
# ---------------------------------------------------------------------------

def test_resolve_area_6_explicit_path() -> None:
    """Resolver returns canonical switch entity when given an explicit path."""
    area_resolver.clear_cache()
    result = area_resolver.resolve("Area 6", str(DATA_PATH))
    assert result == _AREA_6_ENTITY


def test_resolve_unknown_area_raises_with_valid_names() -> None:
    """Unknown area → ValueError listing valid names (explicit path)."""
    area_resolver.clear_cache()
    with pytest.raises(ValueError) as excinfo:
        area_resolver.resolve("Unknown Area", str(DATA_PATH))
    msg = str(excinfo.value)
    assert "Unknown Area" in msg
    assert "Area 6" in msg  # valid names listed


def test_list_areas_returns_eight() -> None:
    """list_areas returns all eight (explicit path)."""
    area_resolver.clear_cache()
    areas = area_resolver.list_areas(str(DATA_PATH))
    assert len(areas) == _TOTAL_AREAS
    names = [a["app_name"] for a in areas]
    for n in range(1, _TOTAL_AREAS + 1):
        assert f"Area {n}" in names


# ---------------------------------------------------------------------------
# Missing-file graceful return
# ---------------------------------------------------------------------------

def test_list_areas_returns_empty_on_missing_file(tmp_path) -> None:
    """list_areas returns empty list when an explicit mapping file is missing."""
    area_resolver.clear_cache()
    missing = tmp_path / "nope.json"
    assert area_resolver.list_areas(str(missing)) == []


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_resolve_caches_mapping_explicit_path() -> None:
    """Mapping is cached by path — repeated calls do not re-read disk."""
    area_resolver.clear_cache()
    area_resolver.resolve("Area 6", str(DATA_PATH))
    cache_info_before = area_resolver._load_mapping.cache_info()  # noqa: SLF001
    area_resolver.resolve("Area 6", str(DATA_PATH))
    cache_info_after = area_resolver._load_mapping.cache_info()  # noqa: SLF001
    assert cache_info_after.hits > cache_info_before.hits


def test_resolve_caches_mapping_none_path() -> None:
    """Package-resource path (None) is also cached."""
    area_resolver.clear_cache()
    area_resolver.resolve("Area 6", None)
    cache_info_before = area_resolver._load_mapping.cache_info()  # noqa: SLF001
    area_resolver.resolve("Area 6", None)
    cache_info_after = area_resolver._load_mapping.cache_info()  # noqa: SLF001
    assert cache_info_after.hits > cache_info_before.hits
