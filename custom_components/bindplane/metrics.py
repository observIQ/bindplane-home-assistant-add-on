"""Snapshot Home Assistant entity states as OTLP gauges."""

from __future__ import annotations

import math
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .exporter import attr

ON_OFF = {"on": 1.0, "off": 0.0}

type Metrics = dict[tuple[str, str], list[dict[str, Any]]]


def snapshot(hass: HomeAssistant, exclude: set[str]) -> Metrics:
    """Return gauge data points keyed by (metric name, unit)."""
    entities, devices, areas = er.async_get(hass), dr.async_get(hass), ar.async_get(hass)
    now = str(time.time_ns())
    metrics: Metrics = {}
    for state in hass.states.async_all():
        if state.entity_id in exclude:
            continue
        if state.domain == "sensor":
            try:
                value = float(state.state)
            except ValueError:
                continue
            if not math.isfinite(value):
                continue
            name = f"homeassistant.sensor.{state.attributes.get('device_class') or 'value'}"
        elif state.state in ON_OFF:
            value = ON_OFF[state.state]
            name = f"homeassistant.{state.domain}.on"
        else:
            continue

        attrs = [attr("entity_id", state.entity_id), attr("friendly_name", state.name)]
        if entry := entities.async_get(state.entity_id):
            area_id = entry.area_id
            if not area_id and entry.device_id and (device := devices.async_get(entry.device_id)):
                area_id = device.area_id
            if area_id and (area := areas.async_get_area(area_id)):
                attrs.append(attr("area", area.name))
        unit = state.attributes.get("unit_of_measurement") or ""
        metrics.setdefault((name, unit), []).append({"timeUnixNano": now, "asDouble": value, "attributes": attrs})
    return metrics


def metrics_payload(resource: list[dict[str, Any]], metrics: Metrics) -> dict[str, Any]:
    """Build an OTLP ExportMetricsServiceRequest."""
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": resource},
                "scopeMetrics": [
                    {
                        "scope": {"name": "homeassistant"},
                        "metrics": [
                            {"name": name, "unit": unit, "gauge": {"dataPoints": points}}
                            for (name, unit), points in metrics.items()
                        ],
                    }
                ],
            }
        ]
    }
