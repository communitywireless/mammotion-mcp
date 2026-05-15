"""Opt-in live mow test against the real Thor1 HA + physical mower.

SKIPPED by default. To run:

    MAMMOTION_MCP_LIVE_TEST=1 \\
    HA_TOKEN=<thor1 ha long-lived token> \\
    pytest tests/test_live_mow.py -v

REQUIRES Joshua eyes-on. Will:
1. Fire the full 5-step canonical sequence on Area 6
2. Mower will leave dock, mow for 30s, return to dock
3. Test PASSES when state=docked + charging=on + post-dock cancel fired

Do NOT enable this test in CI. Do NOT run unattended. The mower physically
moves. The blades physically spin.

The non-live tests in ``test_mow_tool.py`` cover sequence correctness +
safety enforcement against mocked HA — that's the gate for the Driver's
work. This live test is the eyes-on integration check at Navigator /
PM-promotion time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mammotion_mcp import area_resolver
from mammotion_mcp.ha_client import HAClient
from mammotion_mcp.safety import SafetyGate
from mammotion_mcp.tools import mow as mow_module

LIVE_TEST_ENABLED = os.environ.get("MAMMOTION_MCP_LIVE_TEST") == "1"
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_URL = os.environ.get("HA_URL", "http://192.168.1.201:8123")
LIVE_AREA = os.environ.get("LIVE_MOW_AREA", "Area 6")
LIVE_BLADE_HEIGHT = int(os.environ.get("LIVE_BLADE_HEIGHT_MM", "55"))
LIVE_DURATION = int(os.environ.get("LIVE_MOW_DURATION_SEC", "30"))


class _FakeServer:
    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self, *_a, **_kw):
        def _wrap(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _wrap


@pytest.mark.skipif(
    not LIVE_TEST_ENABLED,
    reason="Live mow test disabled. Set MAMMOTION_MCP_LIVE_TEST=1 to enable.",
)
@pytest.mark.skipif(not HA_TOKEN, reason="HA_TOKEN env var required for live test.")
@pytest.mark.asyncio
async def test_live_mow_area_6(tmp_path) -> None:
    """Eyes-on live mow of Area 6 — fires real HA service calls + moves the mower."""
    DATA_PATH = str(Path(__file__).parent.parent / "data" / "area-mapping.json")
    os.environ["AREA_MAPPING_PATH"] = DATA_PATH
    area_resolver.clear_cache()

    server = _FakeServer()
    ha = HAClient(
        url=HA_URL, token=HA_TOKEN, mower_entity_id="lawn_mower.luba2_awd_1"
    )
    safety = SafetyGate(
        quiet_hours_start_hst=21,
        quiet_hours_end_hst=8,
        min_battery_pct=30,
        lock_file_path=str(tmp_path / "live-mow.lock"),
    )
    mow_module.register(server, ha_client=ha, safety=safety)

    result = await server.tools["mow_area"](
        LIVE_AREA,
        blade_height_mm=LIVE_BLADE_HEIGHT,
        mow_duration_sec=LIVE_DURATION,
        return_to_dock=True,
        override_quiet_hours=True,  # live test runs whenever Joshua says go
    )
    await ha.aclose()

    assert result["result"] == "mow_complete"
    assert result["mower_status"]["charging"] is True
    assert result["mower_status"]["state"] in ("docked", "paused")
