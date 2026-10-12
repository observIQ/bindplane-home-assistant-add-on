"""Home Assistant entity states and Supervisor stats as OTLP metrics."""

from __future__ import annotations

from collections import Counter
from functools import partial
import math
import time
from typing import Any

from aiohasupervisor import SupervisorError
from aiohasupervisor.models import AddonState, InstalledAddon

from homeassistant.components.hassio import get_supervisor_client
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback

from .exporter import attr, entity_attrs

ON_OFF = {"on": 1.0, "off": 0.0}
SKIP_ATTRIBUTES = {"supported_features"}
GAUGE, SUM = "gauge", "sum"

type MetricKey = tuple[str, str, str]
type Metrics = dict[MetricKey, list[dict[str, Any]]]


def _add(
    metrics: Metrics,
    now: str,
    name: str,
    value: float,
    attrs: list[dict[str, Any]],
    unit: str = "",
    kind: str = GAUGE,
    start: str | None = None,
) -> None:
    point: dict[str, Any] = {"timeUnixNano": now, "attributes": attrs}
    if kind == SUM:
        point["asInt"] = str(int(value))
        if start:
            point["startTimeUnixNano"] = start
    else:
        point["asDouble"] = float(value)
    metrics.setdefault((name, unit, kind), []).append(point)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class MetricsCollector:
    """Turns Home Assistant state into OTLP metrics."""

    def __init__(self, hass: HomeAssistant, exclude: set[str]) -> None:
        self._hass = hass
        self._exclude = exclude
        self._start = str(time.time_ns())
        self._changes: Counter[str] = Counter()

    @callback
    def async_count_change(self, event: Event[EventStateChangedData]) -> None:
        """Count a state change."""
        self._changes[event.data["entity_id"]] += 1

    def snapshot(self) -> Metrics:
        """Return data points for every entity."""
        now = str(time.time_ns())
        metrics: Metrics = {}

        add = partial(_add, metrics, now)

        for state in self._hass.states.async_all():
            if state.entity_id in self._exclude:
                continue
            attrs = entity_attrs(self._hass, state.entity_id)

            add("homeassistant.entity.available", float(state.state != STATE_UNAVAILABLE), attrs)
            add("homeassistant.entity.last_changed", state.last_changed_timestamp, attrs, "s")
            if changes := self._changes.get(state.entity_id):
                add("homeassistant.entity.state_changes", changes, attrs, kind=SUM, start=self._start)

            unit = state.attributes.get("unit_of_measurement") or ""
            try:
                value = float(state.state)
            except ValueError:
                value = None
            if value is not None:
                if math.isfinite(value):
                    suffix = state.attributes.get("device_class") or "value" if state.domain == "sensor" else "value"
                    add(f"homeassistant.{state.domain}.{suffix}", value, attrs, unit)
            elif state.state in ON_OFF:
                add(f"homeassistant.{state.domain}.on", ON_OFF[state.state], attrs)
            elif state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                add("homeassistant.entity.state", 1.0, [*attrs, attr("homeassistant.state", state.state)])

            for key, raw in state.attributes.items():
                if isinstance(raw, (int, float)) and key not in SKIP_ATTRIBUTES and (value := _number(raw)) is not None:
                    add(f"homeassistant.{state.domain}.attribute.{key}", value, attrs)
        return metrics


def addon_attrs(addon: InstalledAddon) -> list[dict[str, Any]]:
    """Describe an add-on and its container."""
    return [
        attr("container.name", f"addon_{addon.slug}"),
        attr("homeassistant.addon.slug", addon.slug),
        attr("homeassistant.addon.name", addon.name),
        *([attr("homeassistant.addon.version", addon.version)] if addon.version else []),
        attr("homeassistant.addon.version_latest", addon.version_latest),
        attr("homeassistant.addon.update_available", addon.update_available),
        attr("homeassistant.addon.repository", addon.repository),
    ]


async def supervisor_metrics(hass: HomeAssistant) -> Metrics:
    """Return CPU, memory, network and disk stats for Home Assistant, Supervisor and every running add-on."""
    client = get_supervisor_client(hass)
    now = str(time.time_ns())
    metrics: Metrics = {}

    add = partial(_add, metrics, now)

    containers = [
        ([attr("container.name", "homeassistant")], client.homeassistant.stats),
        ([attr("container.name", "hassio_supervisor")], client.supervisor.stats),
    ]
    containers += [
        (addon_attrs(a), partial(client.addons.addon_stats, a.slug))
        for a in await client.addons.list()
        if a.state == AddonState.STARTED
    ]
    for attrs, stats_fn in containers:
        try:
            stats = await stats_fn()
        except SupervisorError:
            continue
        add("container.cpu.utilization", stats.cpu_percent / 100, attrs, "1")
        add("container.memory.usage", stats.memory_usage, attrs, "By")
        add("container.memory.limit", stats.memory_limit, attrs, "By")
        add("container.memory.utilization", stats.memory_percent / 100, attrs, "1")
        add("container.network.io", stats.network_rx, [*attrs, attr("network.io.direction", "receive")], "By", SUM)
        add("container.network.io", stats.network_tx, [*attrs, attr("network.io.direction", "transmit")], "By", SUM)
        add("container.disk.io", stats.blk_read, [*attrs, attr("disk.io.direction", "read")], "By", SUM)
        add("container.disk.io", stats.blk_write, [*attrs, attr("disk.io.direction", "write")], "By", SUM)

    try:
        host = await client.host.info()
    except SupervisorError:
        return metrics
    add("system.filesystem.usage", host.disk_used * 1e9, [attr("system.filesystem.state", "used")], "By")
    add("system.filesystem.usage", host.disk_free * 1e9, [attr("system.filesystem.state", "free")], "By")
    return metrics


def metrics_payload(resource: list[dict[str, Any]], metrics: Metrics) -> dict[str, Any]:
    """Build an OTLP ExportMetricsServiceRequest."""

    def metric(name: str, unit: str, kind: str, points: list[dict[str, Any]]) -> dict[str, Any]:
        if kind == SUM:
            return {
                "name": name,
                "unit": unit,
                "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": points},
            }
        return {"name": name, "unit": unit, "gauge": {"dataPoints": points}}

    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": resource},
                "scopeMetrics": [
                    {
                        "scope": {"name": "homeassistant"},
                        "metrics": [metric(*key, points) for key, points in metrics.items()],
                    }
                ],
            }
        ]
    }
