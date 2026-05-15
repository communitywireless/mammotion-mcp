"""Tests for HAClient — pytest-httpx mocks HA REST endpoints."""

from __future__ import annotations

import pytest

from mammotion_mcp.ha_client import HAClient, MowerStatus


@pytest.fixture
def ha_client() -> HAClient:
    return HAClient(
        url="http://test.example:8123",
        token="dummy-token",
        mower_entity_id="lawn_mower.luba2_awd_1",
    )


# --- call_service ---------------------------------------------------------


@pytest.mark.asyncio
async def test_call_service_posts_correct_body(httpx_mock, ha_client) -> None:
    """call_service posts entity_id + extra fields to the right URL."""
    httpx_mock.add_response(
        method="POST",
        url="http://test.example:8123/api/services/mammotion/start_mow",
        json=[],
        status_code=200,
    )
    await ha_client.call_service(
        "mammotion", "start_mow",
        areas=["switch.luba2_awd_1_area_3439157731089703234"],
        blade_height=55,
    )
    request = httpx_mock.get_request()
    assert request.method == "POST"
    import json
    body = json.loads(request.content)
    assert body["entity_id"] == "lawn_mower.luba2_awd_1"
    assert body["blade_height"] == 55
    assert body["areas"] == ["switch.luba2_awd_1_area_3439157731089703234"]


@pytest.mark.asyncio
async def test_call_service_retries_on_502(httpx_mock, ha_client) -> None:
    """502 transient → retry; eventually success."""
    httpx_mock.add_response(
        method="POST",
        url="http://test.example:8123/api/services/mammotion/cancel_job",
        status_code=502,
    )
    httpx_mock.add_response(
        method="POST",
        url="http://test.example:8123/api/services/mammotion/cancel_job",
        status_code=200,
        json=[],
    )
    # Patch sleep so the test doesn't actually wait
    import asyncio
    orig_sleep = asyncio.sleep

    async def fast_sleep(_s: float) -> None:
        await orig_sleep(0)

    import mammotion_mcp.ha_client as mod
    mod.asyncio.sleep = fast_sleep  # type: ignore[attr-defined]
    try:
        await ha_client.call_service("mammotion", "cancel_job")
    finally:
        mod.asyncio.sleep = orig_sleep  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_call_service_does_not_retry_on_400(httpx_mock, ha_client) -> None:
    """4xx is the caller's bug; surface immediately, no retry."""
    httpx_mock.add_response(
        method="POST",
        url="http://test.example:8123/api/services/mammotion/start_mow",
        status_code=400,
        text="bad area entity id",
    )
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        await ha_client.call_service("mammotion", "start_mow", areas=["junk"])


# --- get_state -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_returns_full_dict(httpx_mock, ha_client) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://test.example:8123/api/states/lawn_mower.luba2_awd_1",
        json={
            "entity_id": "lawn_mower.luba2_awd_1",
            "state": "docked",
            "attributes": {"friendly_name": "Luba2-AWD-1"},
            "last_changed": "2026-05-14T19:00:00+00:00",
        },
    )
    state = await ha_client.get_state()
    assert state["state"] == "docked"
    assert state["last_changed"] == "2026-05-14T19:00:00+00:00"


# --- get_mower_status ---------------------------------------------------


@pytest.mark.asyncio
async def test_get_mower_status_composes_from_multiple_entities(
    httpx_mock, ha_client
) -> None:
    """Verify get_mower_status reads + composes the 7 entities."""
    base = "http://test.example:8123/api/states"
    # mower
    httpx_mock.add_response(
        url=f"{base}/lawn_mower.luba2_awd_1",
        json={"state": "docked", "last_changed": "2026-05-14T19:00:00+00:00"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_activity_mode",
        json={"state": "MODE_READY"},
    )
    httpx_mock.add_response(
        url=f"{base}/binary_sensor.luba2_awd_1_charging",
        json={"state": "on"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_battery",
        json={"state": "100"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_last_error_code",
        json={"state": "5002"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_last_error_time",
        json={"state": "2026-05-14T18:00:00+00:00"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_blade_used_time",
        json={"state": "166.49"},
    )

    status: MowerStatus = await ha_client.get_mower_status()
    assert status.state == "docked"
    assert status.activity_mode == "MODE_READY"
    assert status.charging is True
    assert status.battery_pct == 100
    assert status.last_error_code == 5002
    assert status.last_error_time == "2026-05-14T18:00:00+00:00"
    assert status.blade_used_time_hr == pytest.approx(166.49)
    assert status.last_changed == "2026-05-14T19:00:00+00:00"


@pytest.mark.asyncio
async def test_get_mower_status_handles_individual_entity_failures(
    httpx_mock, ha_client
) -> None:
    """If one sensor returns 404 the composition still succeeds with None."""
    base = "http://test.example:8123/api/states"
    httpx_mock.add_response(
        url=f"{base}/lawn_mower.luba2_awd_1",
        json={"state": "docked", "last_changed": "2026-05-14T19:00:00+00:00"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_activity_mode",
        json={"state": "MODE_READY"},
    )
    httpx_mock.add_response(
        url=f"{base}/binary_sensor.luba2_awd_1_charging",
        json={"state": "on"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_battery",
        json={"state": "100"},
    )
    # last_error_code returns 404 → swallowed → None
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_last_error_code",
        status_code=404,
        text="entity not found",
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_last_error_time",
        json={"state": "2026-05-14T18:00:00+00:00"},
    )
    httpx_mock.add_response(
        url=f"{base}/sensor.luba2_awd_1_blade_used_time",
        json={"state": "166.49"},
    )

    status = await ha_client.get_mower_status()
    assert status.state == "docked"
    assert status.last_error_code is None
    assert status.battery_pct == 100


def test_mower_status_to_dict_includes_protocol_version() -> None:
    s = MowerStatus(
        state="docked", activity_mode="MODE_READY", charging=True,
        battery_pct=100, last_error_code=5002,
        last_error_time="2026-05-14T18:00:00+00:00",
        blade_used_time_hr=166.49,
        last_changed="2026-05-14T19:00:00+00:00",
    )
    d = s.to_dict()
    assert d["protocol_version"] == 1
    assert d["battery_pct"] == 100
    assert d["charging"] is True


# --- HAClient construction ---------------------------------------------


def test_haclient_rejects_empty_token() -> None:
    with pytest.raises(ValueError, match="HA_TOKEN"):
        HAClient(url="http://x", token="", mower_entity_id="lawn_mower.luba2_awd_1")
