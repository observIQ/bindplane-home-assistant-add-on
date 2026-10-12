"""Minimal Bindplane REST API client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import aiohttp

from homeassistant.util.json import json_loads

from .const import (
    CONF_AGENT_ID,
    CONF_API_URL,
    CONF_CONFIGURATION,
    CONF_PROJECT_ID,
    DEFAULT_CONFIGURATION,
    DEV_NULL_DESTINATION,
)

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


def ui_url(api_url: str, project_id: str | None, path: str = "") -> str:
    """Return a Bindplane UI link."""
    base = api_url.rstrip("/")
    return f"{base}/p/{project_id}{path}" if project_id else f"{base}{path or '/'}"


def bindplane_link(data: Mapping[str, Any]) -> str | None:
    """Return the most specific Bindplane UI page for a config entry."""
    if not (api_url := data.get(CONF_API_URL)):
        return None
    project = data.get(CONF_PROJECT_ID)
    if configuration := data.get(CONF_CONFIGURATION):
        return ui_url(api_url, project, f"/configurations/{quote(configuration)}")
    if agent_id := data.get(CONF_AGENT_ID):
        return ui_url(api_url, project, f"/collectors/{agent_id}")
    return ui_url(api_url, project)


def server_url(opamp_endpoint: str) -> str:
    """Return the Bindplane server URL for an OpAMP endpoint."""
    url = opamp_endpoint.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    return url.removesuffix("/").removesuffix("/v1/opamp")


def configuration_resource(name: str, destinations: list[str]) -> dict[str, Any]:
    """Return a v2 Configuration with an OTLP source routed to the given destinations."""
    dest_ids = [f"d-{i}" for i in range(len(destinations))]
    route = [{"id": "0", "components": [f"destinations/{d}" for d in dest_ids]}]
    return {
        "apiVersion": "bindplane.observiq.com/v2",
        "kind": "Configuration",
        "metadata": {
            "name": name,
            "description": "Home Assistant logs, metrics and events over OTLP, plus host and add-on logs from the journal",
            "labels": {"platform": "linux", "agent-type": "observiq-otel-collector"},
        },
        "spec": {
            "sources": [
                {
                    "id": "home-assistant",
                    "type": "otlp",
                    "parameters": [
                        {"name": "telemetry_types", "value": ["Logs", "Metrics", "Traces"]},
                        {"name": "listen_address", "value": "0.0.0.0"},
                        {"name": "grpc_port", "value": 4317},
                        {"name": "http_port", "value": 4318},
                    ],
                    "routes": {"logs": route, "metrics": route, "traces": route},
                },
                {
                    "id": "home-assistant-journal",
                    "type": "journald",
                    "parameters": [
                        {"name": "directory", "value": "/var/log/journal"},
                        {"name": "priority", "value": "info"},
                        {"name": "start_at", "value": "end"},
                    ],
                    "routes": {"logs": route},
                },
            ],
            "destinations": [{"id": i, "name": d} for i, d in zip(dest_ids, destinations, strict=True)],
            "selector": {"matchLabels": {"configuration": name}},
        },
    }


def dev_null_destination() -> dict[str, Any]:
    """Return a Destination that discards everything, for configurations without a real destination yet."""
    return {
        "apiVersion": "bindplane.observiq.com/v1",
        "kind": "Destination",
        "metadata": {"name": DEV_NULL_DESTINATION},
        "spec": {
            "type": "dev_null",
            "parameters": [{"name": "telemetry_types", "value": ["Logs", "Metrics", "Traces"]}],
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
        resources = [configuration_resource(name, destinations or [DEV_NULL_DESTINATION])]
        if not destinations:
            resources.insert(0, dev_null_destination())
        data = await self._request("POST", "/apply", {"resources": resources})
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
