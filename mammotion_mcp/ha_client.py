"""Home Assistant REST API client — wraps ``mammotion.*`` + ``lawn_mower.*`` services.

Tools call this client; tools do NOT compose raw HTTP themselves.

Retry policy: transient errors (network failure, 502/503/504) retry up to
3 times with exponential backoff (1s, 2s, 4s). 4xx errors surface
immediately — they're caller bugs, not infrastructure flakes.

Verified surface (2026-05-14 18:30 HST against Thor1 HA):
- ``sensor.luba2_awd_1_activity_mode`` — state holds enum like "MODE_READY"
- ``sensor.luba2_awd_1_battery`` — state holds 0-100 (int)
- ``binary_sensor.luba2_awd_1_charging`` — state "on"/"off"
- ``sensor.luba2_awd_1_last_error_code`` — state holds int code
- ``sensor.luba2_awd_1_last_error_time`` — state holds ISO timestamp
- ``sensor.luba2_awd_1_blade_used_time`` — state holds hours (float)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger("mammotion_mcp.ha_client")

# Retry config
_MAX_RETRIES = 3
_TRANSIENT_STATUS_CODES = (502, 503, 504)


@dataclass
class MowerStatus:
    """Snapshot of mower state for ``get_mower_status`` tool.

    All fields are best-effort: any sensor that fails to read becomes None
    rather than raising. Callers should treat None as "unknown" not "absent."
    """

    state: str | None
    activity_mode: str | None
    charging: bool
    battery_pct: int | None
    last_error_code: int | None
    last_error_time: str | None
    blade_used_time_hr: float | None
    last_changed: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for MCP responses."""
        out = asdict(self)
        # Hyperscale-readiness #3: include protocol version for forward compat
        out["protocol_version"] = 1
        return out


def _safe_int(value: Any) -> int | None:
    """Coerce a HA state string to int, return None on failure."""
    if value is None or value == "unknown" or value == "unavailable":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    """Coerce a HA state string to float, return None on failure."""
    if value is None or value == "unknown" or value == "unavailable":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str | None:
    """Return the value as a string, or None for empty/unknown."""
    if value is None or value == "unknown" or value == "unavailable":
        return None
    return str(value)


class HAClient:
    """Async HA REST wrapper scoped to mower control.

    Mutating service calls (``call_service``) are only invoked from the
    Tier-1 / Tier-3 / Tier-4 tool bodies — never directly by MCP callers,
    who don't have access to the primitives.
    """

    def __init__(self, *, url: str, token: str, mower_entity_id: str) -> None:
        if not token:
            raise ValueError("HA_TOKEN env var is required")
        self.url = url.rstrip("/")
        self.token = token
        self.mower_entity_id = mower_entity_id
        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """HTTP request with retry on transient failures.

        Retries on httpx.NetworkError or HTTP 502/503/504, up to 3 attempts
        with exponential backoff (1s, 2s, 4s).

        4xx (client error) surfaces immediately — it's a caller bug, not
        infrastructure flake. The retry would just retry-the-same-mistake.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.request(method, url, **kwargs)
                if resp.status_code in _TRANSIENT_STATUS_CODES:
                    LOGGER.warning(
                        "HA REST %s %s returned %d (transient); attempt %d/%d",
                        method, url, resp.status_code, attempt + 1, _MAX_RETRIES,
                    )
                    if attempt == _MAX_RETRIES - 1:
                        resp.raise_for_status()
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.NetworkError as exc:
                last_exc = exc
                LOGGER.warning(
                    "HA REST %s %s network error (%s); attempt %d/%d",
                    method, url, exc, attempt + 1, _MAX_RETRIES,
                )
                if attempt == _MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
        # Shouldn't reach here; raise the last network exception just in case
        assert last_exc is not None  # for type checker
        raise last_exc

    async def call_service(
        self,
        domain: str,
        service: str,
        **service_data: Any,
    ) -> list[dict[str, Any]]:
        """POST to ``/api/services/<domain>/<service>``.

        Args:
            domain: HA service domain (e.g. ``"mammotion"`` or ``"lawn_mower"``).
            service: Service name (e.g. ``"start_mow"``).
            **service_data: Service-specific fields. Always includes
                            ``entity_id`` automatically (the configured mower).

        Returns:
            List of dicts representing entities that changed state as a
            result of the call. Most callers ignore this.

        Raises:
            httpx.HTTPStatusError: on non-transient 4xx/5xx after retries.
            httpx.NetworkError: on persistent network failure after retries.
        """
        body: dict[str, Any] = {"entity_id": self.mower_entity_id, **service_data}
        LOGGER.info("HA call_service: %s.%s body=%s", domain, service, body)
        resp = await self._request_with_retry(
            "POST",
            f"/api/services/{domain}/{service}",
            json=body,
        )
        # HA returns 200 with a list of changed states (may be empty list)
        try:
            return resp.json()
        except ValueError:
            return []

    async def get_state(self, entity_id: str | None = None) -> dict[str, Any]:
        """GET ``/api/states/<entity_id>``.

        Args:
            entity_id: Entity to read. Defaults to the configured mower.

        Returns:
            Dict with ``entity_id``, ``state``, ``attributes``,
            ``last_changed``, etc. If the entity is unknown to HA the
            response is HTTP 404 → raises.
        """
        eid = entity_id or self.mower_entity_id
        resp = await self._request_with_retry("GET", f"/api/states/{eid}")
        return resp.json()

    async def get_mower_status(self) -> MowerStatus:
        """Compose a status snapshot from multiple HA entities.

        Reads (in parallel via asyncio.gather):
        - ``lawn_mower.luba2_awd_1`` (overall state + last_changed)
        - ``sensor.luba2_awd_1_activity_mode``
        - ``binary_sensor.luba2_awd_1_charging``
        - ``sensor.luba2_awd_1_battery``
        - ``sensor.luba2_awd_1_last_error_code``
        - ``sensor.luba2_awd_1_last_error_time``
        - ``sensor.luba2_awd_1_blade_used_time``

        Any individual entity that fails to read becomes None in the
        result — full snapshot is best-effort, not all-or-nothing.
        """
        # Derive sibling entity IDs from the mower entity. The mower entity
        # is ``lawn_mower.luba2_awd_1``; the sibling sensors are
        # ``<sensor|binary_sensor>.luba2_awd_1_*``.
        base = self.mower_entity_id.split(".", 1)[-1]  # "luba2_awd_1"

        entity_ids = {
            "mower": self.mower_entity_id,
            "activity_mode": f"sensor.{base}_activity_mode",
            "charging": f"binary_sensor.{base}_charging",
            "battery": f"sensor.{base}_battery",
            "last_error_code": f"sensor.{base}_last_error_code",
            "last_error_time": f"sensor.{base}_last_error_time",
            "blade_used": f"sensor.{base}_blade_used_time",
        }

        async def _safe_get(eid: str) -> dict[str, Any] | None:
            try:
                return await self.get_state(eid)
            except (httpx.HTTPError, ValueError) as exc:
                LOGGER.warning("Failed to read %s: %s", eid, exc)
                return None

        results = await asyncio.gather(
            *[_safe_get(eid) for eid in entity_ids.values()]
        )
        states = dict(zip(entity_ids.keys(), results))

        mower = states["mower"] or {}
        return MowerStatus(
            state=_safe_str((mower).get("state")),
            activity_mode=_safe_str(
                (states["activity_mode"] or {}).get("state")
            ),
            charging=(states["charging"] or {}).get("state") == "on",
            battery_pct=_safe_int((states["battery"] or {}).get("state")),
            last_error_code=_safe_int(
                (states["last_error_code"] or {}).get("state")
            ),
            last_error_time=_safe_str(
                (states["last_error_time"] or {}).get("state")
            ),
            blade_used_time_hr=_safe_float(
                (states["blade_used"] or {}).get("state")
            ),
            last_changed=_safe_str((mower).get("last_changed")),
        )

    async def safe_float_state(self, entity_id: str) -> float | None:
        """Read an entity's ``state`` as a float, or None if unavailable.

        Convenience helper for v1.1 verification — most sensors return their
        state as a string that has to be coerced. Numeric sensors like
        ``sensor.luba2_awd_1_blade_used_time`` can also briefly report
        ``"unknown"`` or ``"unavailable"`` after MQTT drops; this helper
        returns None in those cases instead of raising.

        Network and HTTP errors propagate unchanged so callers can apply
        their own retry/abort logic (e.g., ``_verify_mowing`` distinguishes
        environmental failures from verification failures).

        Args:
            entity_id: Entity to read (e.g. ``"sensor.luba2_awd_1_blade_used_time"``).

        Returns:
            Float value of the entity's ``state``, or None if the state was
            ``unknown`` / ``unavailable`` / missing / non-numeric.
        """
        state_obj = await self.get_state(entity_id)
        return _safe_float(state_obj.get("state"))

    async def reload_config_entry(self, entry_id: str) -> dict[str, Any]:
        """Reload a HA config entry via POST.

        ``POST /api/config/config_entries/entry/<entry_id>/reload``

        Used by ``reload_integration`` Tier-4 diagnostic tool. The entry
        ID is discovered at startup and cached.
        """
        resp = await self._request_with_retry(
            "POST",
            f"/api/config/config_entries/entry/{entry_id}/reload",
        )
        try:
            return resp.json()
        except ValueError:
            return {}

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Idempotent."""
        await self._client.aclose()
