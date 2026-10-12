"""Minimal Bindplane REST API client."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.util.json import json_loads

from .const import DEFAULT_CONFIGURATION

AGENT_STATUS = {
    0: "disconnected",
    1: "connected",
    2: "error",
    4: "component_failed",
    5: "deleted",
    6: "configuring",
    7: "upgrading",
}


class BindplaneError(Exception):
    """Bindplane API request failed."""


class BindplaneAuthError(BindplaneError):
    """Bindplane rejected the API key."""


class BindplanePermissionError(BindplaneError):
    """The API key lacks a permission."""


def _error_text(body: str) -> str:
    try:
        return "; ".join(json_loads(body)["errors"])
    except (ValueError, KeyError, TypeError):
        return body[:300]


def opamp_endpoint(api_url: str) -> str:
    """Return the OpAMP websocket URL for a Bindplane server URL."""
    return api_url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/v1/opamp"


def configuration_resource(name: str, destinations: list[str]) -> dict[str, Any]:
    """Return a Configuration with an OTLP source feeding the given destinations."""
    return {
        "apiVersion": "bindplane.observiq.com/v1",
        "kind": "Configuration",
        "metadata": {
            "name": name,
            "description": "Home Assistant logs and events over OTLP",
            "labels": {"platform": "linux", "agent-type": "observiq-otel-collector"},
        },
        "spec": {
            "sources": [
                {
                    "type": "otlp",
                    "parameters": [
                        {"name": "telemetry_types", "value": ["Logs", "Metrics", "Traces"]},
                        {"name": "listen_address", "value": "0.0.0.0"},
                        {"name": "grpc_port", "value": 4317},
                        {"name": "http_port", "value": 4318},
                    ],
                }
            ],
            "destinations": [{"name": d} for d in destinations],
            "selector": {"matchLabels": {"configuration": name}},
        },
    }


class BindplaneClient:
    """Talks to the Bindplane REST API."""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str, project_id: str | None = None) -> None:
        self._session = session
        self._url = url.rstrip("/") + "/v1"
        self._headers = {"X-Bindplane-Api-Key": api_key}
        if project_id:
            self._headers["X-Bindplane-Account-ID"] = project_id

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            async with self._session.request(
                method, self._url + path, json=json, headers=self._headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 401:
                    raise BindplaneAuthError(_error_text(await resp.text()))
                if resp.status == 403:
                    raise BindplanePermissionError(_error_text(await resp.text()))
                if resp.status >= 400:
                    raise BindplaneError(f"{resp.status}: {_error_text(await resp.text())}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BindplaneError(str(err)) from err

    async def secret_key(self) -> str:
        """Return the project's default secret key."""
        return (await self._request("GET", "/secret-keys"))["defaultSecretKey"]

    async def destinations(self) -> list[str]:
        """Return destination names."""
        data = await self._request("GET", "/destinations")
        return sorted(d["metadata"]["name"] for d in data.get("destinations") or [])

    async def configurations(self) -> list[str]:
        """Return configuration names."""
        data = await self._request("GET", "/configurations")
        return sorted(c["metadata"]["name"] for c in data.get("configurations") or [])

    async def apply_configuration(self, destinations: list[str], name: str = DEFAULT_CONFIGURATION) -> None:
        """Create or update the Home Assistant configuration and roll it out."""
        data = await self._request("POST", "/apply", {"resources": [configuration_resource(name, destinations)]})
        for update in data.get("updates") or []:
            if update.get("status") == "forbidden":
                raise BindplanePermissionError(update.get("reason") or "forbidden")
            if update.get("status") in ("invalid", "error"):
                raise BindplaneError(update.get("reason") or update["status"])
        await self._request("POST", f"/rollouts/{name}/start", {})

    async def collector(self, agent_id: str) -> dict[str, Any] | None:
        """Return a collector, or None if Bindplane hasn't seen it yet."""
        try:
            return (await self._request("GET", f"/collectors/{agent_id}"))["agent"]
        except BindplaneError as err:
            if str(err).startswith("404"):
                return None
            raise
