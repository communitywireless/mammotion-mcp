"""Tests for motion tool registration gating.

Verifies that pause_mow + resume_mow are ALWAYS registered when
motion.register() is called, regardless of the diagnostic flag, while
manual_drive is ONLY registered when enable_diagnostic_tools=True.

This guards against the regression Navigator §7a caught — where pause/resume
were mistakenly gated behind ENABLE_DIAGNOSTIC_TOOLS=true alongside
manual_drive.
"""

from __future__ import annotations

import pytest

from mammotion_mcp.ha_client import HAClient
from mammotion_mcp.safety import SafetyGate
from mammotion_mcp.tools import motion as motion_module


class _FakeServer:
    """Captures tool decorators so we can inspect what got registered."""

    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self, *_a, **_kw):  # mimic FastMCP.tool decorator
        def _wrap(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _wrap


@pytest.fixture
def fake_ha() -> HAClient:
    # We don't make any HA calls — registration alone is being tested.
    return HAClient(
        url="http://localhost:0",
        token="fake",
        mower_entity_id="lawn_mower.test",
    )


@pytest.fixture
def fake_gate(tmp_path) -> SafetyGate:
    return SafetyGate(
        quiet_hours_start_hst=21,
        quiet_hours_end_hst=8,
        min_battery_pct=30,
        lock_file_path=str(tmp_path / "test.lock"),
    )


def test_motion_register_with_diag_off_includes_pause_resume(fake_ha, fake_gate) -> None:
    """pause_mow + resume_mow MUST register without diag flag."""
    server = _FakeServer()
    motion_module.register(
        server,
        ha_client=fake_ha,
        safety=fake_gate,
        enable_diagnostic_tools=False,
    )

    assert "pause_mow" in server.tools, "pause_mow must register when diag flag off"
    assert "resume_mow" in server.tools, "resume_mow must register when diag flag off"
    assert "manual_drive" not in server.tools, "manual_drive MUST NOT register when diag flag off"


def test_motion_register_with_diag_on_includes_manual_drive(fake_ha, fake_gate) -> None:
    """manual_drive MUST register only with diag flag on; pause/resume still register."""
    server = _FakeServer()
    motion_module.register(
        server,
        ha_client=fake_ha,
        safety=fake_gate,
        enable_diagnostic_tools=True,
    )

    assert "pause_mow" in server.tools, "pause_mow must register when diag flag on"
    assert "resume_mow" in server.tools, "resume_mow must register when diag flag on"
    assert "manual_drive" in server.tools, "manual_drive MUST register when diag flag on"


def test_motion_register_default_excludes_manual_drive(fake_ha, fake_gate) -> None:
    """Default (no enable_diagnostic_tools kwarg) is the safe-by-default case."""
    server = _FakeServer()
    motion_module.register(server, ha_client=fake_ha, safety=fake_gate)

    assert "pause_mow" in server.tools
    assert "resume_mow" in server.tools
    assert "manual_drive" not in server.tools, "default must NOT register manual_drive"
