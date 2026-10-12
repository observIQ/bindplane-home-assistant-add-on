"""Buffer Home Assistant log records and events and export them as OTLP/HTTP JSON."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import logging
import threading
import time
import traceback
from typing import Any

import aiohttp

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.json import json_dumps
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
    return record.name, {
        "timeUnixNano": str(int(record.created * 1e9)),
        "observedTimeUnixNano": str(time.time_ns()),
        "severityNumber": SEVERITY.get(record.levelno, 9),
        "severityText": record.levelname,
        "body": {"stringValue": record.getMessage()},
        "attributes": attrs,
    }


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


def event_record(event: Event) -> OtlpRecord:
    """Convert a Home Assistant event to an OTLP LogRecord."""
    data = json_loads(json_dumps(event.data))
    return EVENT_SCOPE, {
        "timeUnixNano": str(int(event.time_fired_timestamp * 1e9)),
        "observedTimeUnixNano": str(time.time_ns()),
        "severityNumber": 9,
        "severityText": "INFO",
        "eventName": event.event_type,
        "body": {"stringValue": event_message(event)},
        "attributes": [
            attr("event.name", event.event_type),
            attr("event.origin", event.origin.value),
            *flatten("event.data", data),
            *flatten("event.context", {"id": event.context.id, "user_id": event.context.user_id}),
        ],
    }


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
        self.rejected = 0
        self.status = "pending"
        self.last_error: str | None = None
        self._resource = resource
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
        self.handler.add(event_record(event))

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
