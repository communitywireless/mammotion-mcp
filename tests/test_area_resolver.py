"""Test area-name → switch entity resolution.

v0 scaffold — Driver fleshes out as the resolver lands.
"""

from __future__ import annotations

import json
from pathlib import Path

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
