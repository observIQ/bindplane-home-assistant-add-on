"""Diagnostics for Bindplane."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import BindplaneConfigEntry
from .const import CONF_API_KEY, CONF_PROJECT_ID, CONF_SECRET_KEY

REDACT = {CONF_API_KEY, CONF_PROJECT_ID, CONF_SECRET_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: BindplaneConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    exporter = entry.runtime_data.exporter
    coordinator = entry.runtime_data.coordinator
    return {
        "entry": async_redact_data(entry.as_dict(), REDACT),
        "exporter": {
            "url": exporter.url,
            "status": exporter.status,
            "last_error": exporter.last_error,
            "exported": exporter.exported,
            "metrics_exported": exporter.metrics_exported,
            "spans_exported": exporter.spans_exported,
            "dropped": exporter.dropped,
            "buffered": len(exporter.handler.buffer),
        },
        "collector": coordinator.data if coordinator else None,
    }
