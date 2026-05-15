"""Home Assistant REST API client — wraps `mammotion.*` + `lawn_mower.*` services.

Tools call this client; tools do NOT compose raw HTTP themselves.

v0 scaffold — Driver fleshes out the typed wrappers per the locked surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger("mammotion_mcp.ha_client")


@dataclass
class MowerStatus:
    """Snapshot of mower state for `get_mower_status` tool."""

    state: str | None
    activity_mode: int | None
    charging: bool
    battery_pct: int | None
    last_error_code: int | None
    last_error_time: str | None
    blade_used_time_hr: float | None
    last_changed: str | None


class HAClient:
    """Thin async HA REST wrapper scoped to mower control.

    All methods that mutate device state should be invoked only from inside
    the canonical sequence wrappers in `tools/mow.py` — NOT exposed directly
    to MCP callers.
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

    async def call_service(self, domain: str, service: str, **service_data: Any) -> dict:
        """POST to /api/services/<domain>/<service>."""
        body = {"entity_id": self.mower_entity_id, **service_data}
        resp = await self._client.post(
            f"/api/services/{domain}/{service}",
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_state(self, entity_id: str | None = None) -> dict:
        """GET /api/states/<entity_id>."""
        eid = entity_id or self.mower_entity_id
        resp = await self._client.get(f"/api/states/{eid}")
        resp.raise_for_status()
        return resp.json()

    async def get_mower_status(self) -> MowerStatus:
        """Compose the snapshot from multiple HA entities."""
        # Driver implements full body per Investigator-confirmed field paths
        raise NotImplementedError("Driver: implement against locked surface")

    async def aclose(self) -> None:
        await self._client.aclose()
