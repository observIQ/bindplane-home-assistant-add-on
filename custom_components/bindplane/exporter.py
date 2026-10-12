"""Buffer Home Assistant log records and events and export them as OTLP/HTTP JSON."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import hashlib
import logging
import threading
import time
import traceback
from typing import Any

import aiohttp

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.json import json_dumps
from homeassistant.helpers.trace import trace_id_get, trace_path_get
from homeassistant.util.json import json_loads

BATCH_SIZE = 500
BUFFER_SIZE = 10_000
EVENT_SCOPE = "homeassistant.events"
SEVERITY = {logging.DEBUG: 5, logging.INFO: 9, logging.WARNING: 13, logging.ERROR: 17, logging.CRITICAL: 21}

type OtlpRecord = tuple[str, dict[str, Any]]


def attr(key: str, value: Any) -> dict[str, Any]:
    """Return an OTLP KeyValue."""
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def span_id(run_id: str, key: str) -> str:
    """Return the span ID for a step of an automation or script run."""
    return hashlib.sha256(f"{run_id}/{key}".encode()).hexdigest()[:16]


def trace_link(record: dict[str, Any]) -> dict[str, Any]:
    """Link a record to the automation or script run it was emitted in."""
    if run := trace_id_get():
        record |= {"traceId": run[1], "spanId": span_id(run[1], "")}
        if path := trace_path_get():
            record["attributes"].append(attr("homeassistant.step.path", path))
    return record


def flatten(prefix: str, value: Any, depth: int = 4) -> list[dict[str, Any]]:
    """Flatten nested dicts into dotted OTLP attributes."""
    if isinstance(value, dict) and depth:
        return [a for k, v in value.items() for a in flatten(f"{prefix}.{k}", v, depth - 1)]
    if isinstance(value, (list, dict)):
        return [attr(prefix, json_dumps(value))]
    if value is None:
        return []
    return [attr(prefix, value)]


def log_record(record: logging.LogRecord) -> OtlpRecord:
    """Convert a Python log record to an OTLP LogRecord."""
    attrs = [
        attr("code.file.path", record.pathname),
        attr("code.line.number", record.lineno),
        attr("code.function.name", record.funcName),
        attr("thread.id", record.thread),
        attr("thread.name", record.threadName),
    ]
    if record.exc_info and record.exc_info[0]:
        exc_type, exc, tb = record.exc_info
        attrs += [
            attr("exception.type", exc_type.__name__),
            attr("exception.message", str(exc)),
            attr("exception.stacktrace", "".join(traceback.format_exception(exc_type, exc, tb))),
        ]
    if record.stack_info:
        attrs.append(attr("code.stacktrace", record.stack_info))
    return record.name, trace_link(
        {
            "timeUnixNano": str(int(record.created * 1e9)),
            "observedTimeUnixNano": str(time.time_ns()),
            "severityNumber": SEVERITY.get(record.levelno, 9),
            "severityText": record.levelname,
            "body": {"stringValue": record.getMessage()},
            "attributes": attrs,
        }
    )


def event_message(event: Event) -> str:
    """Return a human readable summary of an event."""
    data = event.data
    if event.event_type == "state_changed" and data.get("new_state"):
        old = data.get("old_state")
        return f"{data['entity_id']}: {old.state if old else None} → {data['new_state'].state}"
    if event.event_type == "call_service":
        return f"Called {data.get('domain')}.{data.get('service')}"
    if event.event_type == "automation_triggered":
        return f"Automation {data.get('name')} triggered by {data.get('source')}"
    return event.event_type


def entity_attrs(hass: HomeAssistant | None, entity_id: str) -> list[dict[str, Any]]:
    """Describe an entity, its device and its area."""
    domain, object_id = entity_id.split(".", 1)
    info: dict[str, Any] = {"entity.id": entity_id, "entity.domain": domain, "entity.object_id": object_id}
    if hass and (state := hass.states.get(entity_id)):
        info |= {"entity.name": state.name, "entity.device_class": state.attributes.get("device_class")}
    if hass and (entry := er.async_get(hass).async_get(entity_id)):
        labels = [label.name for label_id in entry.labels if (label := lr.async_get(hass).async_get_label(label_id))]
        info |= {
            "entity.platform": entry.platform,
            "entity.unique_id": entry.unique_id,
            "entity.labels": ",".join(sorted(labels)) or None,
        }
        device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
        if device:
            info |= {
                "device.id": device.id,
                "device.name": device.name_by_user or device.name,
                "device.manufacturer": device.manufacturer,
                "device.model": device.model,
                "device.sw_version": device.sw_version,
            }
        if (area_id := entry.area_id or (device and device.area_id)) and (
            area := ar.async_get(hass).async_get_area(area_id)
        ):
            info |= {"area.id": area.id, "area.name": area.name}
            if area.floor_id and (floor := fr.async_get(hass).async_get_floor(area.floor_id)):
                info["floor.name"] = floor.name
    return flatten("homeassistant", info)


def event_record(event: Event, hass: HomeAssistant | None = None) -> OtlpRecord:
    """Convert a Home Assistant event to an OTLP LogRecord."""
    data = json_loads(json_dumps(event.data))
    attrs = flatten(
        "homeassistant",
        {
            "event.type": event.event_type,
            "event.origin": event.origin.value,
            "context.id": event.context.id,
            "context.parent_id": event.context.parent_id,
        },
    )
    if event.context.user_id:
        attrs.append(attr("user.id", event.context.user_id))
    if isinstance(entity_id := data.get("entity_id"), str) and "." in entity_id:
        attrs += entity_attrs(hass, data.pop("entity_id"))
    if event.event_type == EVENT_STATE_CHANGED:
        old, new = data.pop("old_state", None) or {}, data.pop("new_state", None) or {}
        old_attrs, new_attrs = old.get("attributes", {}), new.get("attributes", {})
        attrs += flatten(
            "homeassistant.state",
            {
                "old": old.get("state"),
                "new": new.get("state"),
                "last_changed": new.get("last_changed"),
                "last_updated": new.get("last_updated"),
                "old_last_changed": old.get("last_changed"),
                "attributes": new_attrs,
                "old_attributes": {k: v for k, v in old_attrs.items() if new_attrs.get(k) != v},
            },
        )
    return EVENT_SCOPE, trace_link(
        {
            "timeUnixNano": str(int(event.time_fired_timestamp * 1e9)),
            "observedTimeUnixNano": str(time.time_ns()),
            "severityNumber": 9,
            "severityText": "INFO",
            "eventName": event.event_type,
            "body": {"stringValue": event_message(event)},
            "attributes": [*attrs, *flatten("homeassistant.event.data", data)],
        }
    )


def payload(resource: list[dict[str, Any]], records: list[OtlpRecord]) -> dict[str, Any]:
    """Build an OTLP ExportLogsServiceRequest, one scope per logger."""
    scopes: dict[str, list[dict[str, Any]]] = {}
    for scope, rec in records:
        scopes.setdefault(scope, []).append(rec)
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": resource},
                "scopeLogs": [{"scope": {"name": name}, "logRecords": recs} for name, recs in scopes.items()],
            }
        ]
    }


class OtlpHandler(logging.Handler):
    """Logging handler that buffers records for the exporter."""

    def __init__(self, level: int, exclude: list[str]) -> None:
        super().__init__(level)
        self.buffer: deque[OtlpRecord] = deque(maxlen=BUFFER_SIZE)
        self.dropped = 0
        self._exclude = (*exclude, __package__)
        self._local = threading.local()

    def add(self, record: OtlpRecord) -> None:
        """Buffer a record, dropping the oldest when full."""
        if len(self.buffer) == BUFFER_SIZE:
            self.dropped += 1
        self.buffer.append(record)

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer a log record."""
        if record.name.startswith(self._exclude) or getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            self.add(log_record(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)
        finally:
            self._local.busy = False


class Exporter:
    """Periodically posts buffered records to the collector."""

    def __init__(self, hass: HomeAssistant, url: str, resource: list[dict[str, Any]], handler: OtlpHandler) -> None:
        self._base = url.rstrip("/")
        self.url = self._base + "/v1/logs"
        self.handler = handler
        self.exported = 0
        self.metrics_exported = 0
        self.spans_exported = 0
        self.rejected = 0
        self.status = "pending"
        self.last_error: str | None = None
        self._resource = resource
        self._hass = hass
        self._session = async_get_clientsession(hass)
        self._pending: list[OtlpRecord] = []
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []

    @property
    def dropped(self) -> int:
        """Records lost to a full buffer or rejected by the collector."""
        return self.handler.dropped + self.rejected

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to export results."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_add_event(self, event: Event) -> None:
        """Buffer a Home Assistant event."""
        self.handler.add(event_record(event, self._hass))

    def _set(self, status: str, error: str | None = None) -> None:
        self.status, self.last_error = status, error

    async def post(self, path: str, body: dict[str, Any]) -> int | None:
        """POST an OTLP request; return the HTTP status, or None if the collector is unreachable."""
        try:
            async with self._session.post(
                self._base + path,
                data=json_dumps(body),
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            self._set("error", f"{type(err).__name__}: {err}" if str(err) else type(err).__name__)
            return None
        if status >= 400:
            self._set("error", f"HTTP {status}: {text[:200]}")
        else:
            self._set("ok")
        return status

    async def flush(self, *_: Any) -> None:
        """Send everything buffered."""
        async with self._lock:
            buffer = self.handler.buffer
            while self._pending or buffer:
                if not self._pending:
                    self._pending = [buffer.popleft() for _ in range(min(BATCH_SIZE, len(buffer)))]
                status = await self.post("/v1/logs", payload(self._resource, self._pending))
                if status is None or status >= 500 or status == 429:
                    break
                if status >= 400:
                    self.rejected += len(self._pending)
                else:
                    self.exported += len(self._pending)
                self._pending = []
        self.notify()

    @callback
    def notify(self) -> None:
        """Tell listeners the stats changed."""
        for listener in self._listeners:
            listener()
