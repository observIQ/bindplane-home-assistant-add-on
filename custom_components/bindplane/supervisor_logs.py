"""Follow Supervisor, host and add-on logs through the Supervisor API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import os
import re
import time
from typing import Any

from aiohasupervisor import SupervisorError
from aiohasupervisor.models import AddonState
import aiohttp

from homeassistant.components.hassio import get_supervisor_client
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .exporter import OtlpHandler, attr

_LOGGER = logging.getLogger(__name__)

LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (\S*) ([^\s\[:]+)(?:\[(\d+)\])?: (.*)$")
LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|CRIT|FATAL)\b", re.IGNORECASE)
SEVERITY = {
    "TRACE": (1, "TRACE"),
    "DEBUG": (5, "DEBUG"),
    "INFO": (9, "INFO"),
    "NOTICE": (10, "INFO"),
    "WARN": (13, "WARNING"),
    "WARNING": (13, "WARNING"),
    "ERR": (17, "ERROR"),
    "ERROR": (17, "ERROR"),
    "CRIT": (21, "CRITICAL"),
    "CRITICAL": (21, "CRITICAL"),
    "FATAL": (21, "CRITICAL"),
}
FLUSH_AFTER = 1.0
RETRY_AFTER = 10


@dataclass
class Entry:
    """One journal entry, possibly spanning several lines."""

    timestamp: str
    identifier: str
    pid: str | None
    lines: list[str] = field(default_factory=list)


def parse_line(line: str) -> Entry | None:
    """Parse a verbose Supervisor log line, or None for a continuation line."""
    if not (match := LINE.match(line)):
        return None
    timestamp, _host, identifier, pid, message = match.groups()
    return Entry(timestamp, identifier, pid, [message])


def log_record(entry: Entry, source: str, attrs: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Convert a journal entry to an OTLP LogRecord."""
    message = "\n".join(entry.lines)
    ts = datetime.strptime(entry.timestamp, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    record: dict[str, Any] = {
        "timeUnixNano": str(int(ts.timestamp() * 1e9)),
        "observedTimeUnixNano": str(time.time_ns()),
        "body": {"stringValue": message},
        "attributes": [
            *attrs,
            attr("syslog.identifier", entry.identifier),
            *([attr("process.pid", int(entry.pid))] if entry.pid else []),
        ],
    }
    if level := LEVEL.search(message[:120]):
        record["severityNumber"], record["severityText"] = SEVERITY[level.group(1).upper()]
    return f"homeassistant.supervisor.{source}", record


class SupervisorLogs:
    """Streams logs from Supervisor into the OTLP handler."""

    def __init__(self, hass: HomeAssistant, handler: OtlpHandler) -> None:
        self._hass = hass
        self._handler = handler
        self._session = async_get_clientsession(hass)
        self._url = f"http://{os.environ.get('SUPERVISOR', 'supervisor')}"
        self._headers = {
            "Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}",
            "Accept": "text/x-log",
        }
        self._since = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._tasks: dict[str, asyncio.Task] = {}

    async def async_refresh(self, *_: Any) -> None:
        """Follow Supervisor, the host and every running add-on."""
        streams = {
            "supervisor": ("/supervisor/logs/follow", [attr("container.name", "hassio_supervisor")]),
            "host": ("/host/logs/follow", []),
        }
        try:
            addons = await get_supervisor_client(self._hass).addons.list()
        except SupervisorError as err:
            _LOGGER.debug("Could not list add-ons: %s", err)
        else:
            for addon in addons:
                if addon.state == AddonState.STARTED:
                    streams[f"addon.{addon.slug}"] = (
                        f"/addons/{addon.slug}/logs/follow",
                        [
                            attr("container.name", f"addon_{addon.slug}"),
                            attr("homeassistant.addon.slug", addon.slug),
                            attr("homeassistant.addon.name", addon.name),
                        ],
                    )
        for source in set(self._tasks) - set(streams):
            self._tasks.pop(source).cancel()
        for source, (path, attrs) in streams.items():
            if source not in self._tasks or self._tasks[source].done():
                self._tasks[source] = self._hass.async_create_background_task(
                    self._follow(source, path, attrs), f"bindplane {source} logs"
                )

    def stop(self) -> None:
        """Stop following logs."""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _follow(self, source: str, path: str, attrs: list[dict[str, Any]]) -> None:
        last = self._since
        seen: set[str] = set()
        while True:
            pending: Entry | None = None

            def emit() -> None:
                nonlocal pending, last, seen
                if pending is None:
                    return
                key = "\n".join(pending.lines)
                if pending.timestamp > last or (pending.timestamp == last and key not in seen):
                    if pending.timestamp > last:
                        last, seen = pending.timestamp, set()
                    seen.add(key)
                    self._handler.add(log_record(pending, source, attrs))
                pending = None

            try:
                async with self._session.get(
                    self._url + path,
                    headers=self._headers,
                    params={"no_colors": ""},
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                ) as resp:
                    resp.raise_for_status()
                    content = resp.content
                    while True:
                        try:
                            raw = await asyncio.wait_for(content.readline(), FLUSH_AFTER)
                        except TimeoutError:
                            emit()
                            continue
                        if not raw:
                            break
                        line = raw.decode(errors="replace").rstrip("\n")
                        if entry := parse_line(line):
                            emit()
                            pending = entry
                        elif pending is not None:
                            pending.lines.append(line)
                    emit()
            except (aiohttp.ClientError, TimeoutError) as err:
                emit()
                _LOGGER.debug("%s log stream ended: %s", source, err)
            await asyncio.sleep(RETRY_AFTER)
