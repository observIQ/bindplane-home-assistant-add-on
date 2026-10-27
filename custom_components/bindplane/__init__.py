"""Ship Home Assistant logs, metrics and events to a Bindplane-managed OpenTelemetry collector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import platform
import socket
from typing import Any

from aiohasupervisor import SupervisorError
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    MATCH_ALL,
    Platform,
    __version__ as HA_VERSION,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.system_info import async_get_system_info
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BindplaneAuthError, BindplaneClient, BindplaneError
from .const import (
    ALL_EVENTS,
    CONF_AGENT_ID,
    CONF_API_KEY,
    CONF_API_URL,
    CONF_EVENTS,
    CONF_EXCLUDE,
    CONF_LEVEL,
    CONF_METRICS,
    CONF_METRICS_EXCLUDE,
    CONF_OTLP_URL,
    CONF_PROJECT_ID,
    CONF_SUPERVISOR_LOGS,
    CONF_TRACES,
    DEFAULT_EVENTS,
    DEFAULT_LEVEL,
    DOMAIN,
)
from .exporter import Exporter, OtlpHandler, attr, flatten
from .metrics import MetricsCollector, metrics_payload, supervisor_metrics
from .supervisor_logs import SupervisorLogs
from .traces import TraceCollector, traces_payload

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
FLUSH_INTERVAL = timedelta(seconds=5)
METRICS_INTERVAL = timedelta(seconds=60)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type BindplaneConfigEntry = ConfigEntry[BindplaneData]


@dataclass
class BindplaneData:
    """Runtime data for a config entry."""

    exporter: Exporter
    coordinator: DataUpdateCoordinator[dict[str, Any] | None] | None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Bindplane actions."""

    def loaded() -> list[BindplaneConfigEntry]:
        return [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]

    async def flush(call: ServiceCall) -> None:
        for entry in loaded():
            await entry.runtime_data.exporter.flush()

    async def send_test(call: ServiceCall) -> None:
        record = logging.LogRecord("bindplane.test", logging.WARNING, __file__, 0, call.data["message"], None, None)
        for entry in loaded():
            entry.runtime_data.exporter.handler.handle(record)
            await entry.runtime_data.exporter.flush()

    hass.services.async_register(DOMAIN, "flush", flush)
    hass.services.async_register(
        DOMAIN,
        "send_test",
        send_test,
        vol.Schema({vol.Optional("message", default="Hello from Home Assistant 👋"): cv.string}),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BindplaneConfigEntry) -> bool:
    """Attach the OTLP log handler and start exporting."""
    resource = [
        attr("service.name", "home-assistant"),
        attr("service.version", HA_VERSION),
        attr("service.instance.id", await async_get_instance_id(hass)),
        attr("host.name", socket.gethostname()),
        attr("host.arch", {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())),
        attr("os.type", platform.system().lower()),
        attr("os.version", platform.release()),
        *flatten(
            "homeassistant",
            {
                "location_name": hass.config.location_name,
                "time_zone": hass.config.time_zone,
                "country": hass.config.country,
                "installation_type": (await async_get_system_info(hass)).get("installation_type"),
            },
        ),
    ]
    options = entry.options
    handler = OtlpHandler(logging.getLevelName(options.get(CONF_LEVEL, DEFAULT_LEVEL)), options.get(CONF_EXCLUDE, []))
    exporter = Exporter(hass, entry.data[CONF_OTLP_URL], resource, handler)

    root = logging.getLogger()
    if handler.level < root.level:
        previous = root.level
        quieted = [(h, h.level) for h in root.handlers if h.level < previous]
        for h, _ in quieted:
            h.setLevel(previous)
        root.setLevel(handler.level)

        def restore_levels() -> None:
            root.setLevel(previous)
            for h, level in quieted:
                h.setLevel(level)

        entry.async_on_unload(restore_levels)
    root.addHandler(handler)
    entry.async_on_unload(lambda: root.removeHandler(handler))
    entry.async_on_unload(async_track_time_interval(hass, exporter.flush, FLUSH_INTERVAL, name="bindplane flush"))
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, exporter.flush))

    events = options.get(CONF_EVENTS, DEFAULT_EVENTS)
    if ALL_EVENTS in events:
        skip = set() if EVENT_STATE_CHANGED in events else {EVENT_STATE_CHANGED}

        @callback
        def forward(event: Event) -> None:
            if event.event_type not in skip:
                exporter.async_add_event(event)

        entry.async_on_unload(hass.bus.async_listen(MATCH_ALL, forward))
    else:
        for event_type in events:
            entry.async_on_unload(hass.bus.async_listen(event_type, exporter.async_add_event))

    if is_hassio(hass) and options.get(CONF_SUPERVISOR_LOGS, True):
        supervisor_logs = SupervisorLogs(hass, handler)
        entry.async_on_unload(supervisor_logs.stop)
        entry.async_on_unload(async_at_started(hass, supervisor_logs.async_refresh))
        entry.async_on_unload(
            async_track_time_interval(
                hass, supervisor_logs.async_refresh, METRICS_INTERVAL, name="bindplane log streams"
            )
        )

    if options.get(CONF_TRACES, True):
        traces = TraceCollector(hass)

        async def export_traces(*_: Any) -> None:
            if not (spans := traces.collect()):
                return
            status = await exporter.post("/v1/traces", traces_payload(resource, spans))
            if status is not None and status < 400:
                exporter.spans_exported += len(spans)
            exporter.notify()

        entry.async_on_unload(async_track_time_interval(hass, export_traces, FLUSH_INTERVAL, name="bindplane traces"))

    if options.get(CONF_METRICS, True):
        collector = MetricsCollector(hass, set(options.get(CONF_METRICS_EXCLUDE, [])))
        entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, collector.async_count_change))

        async def export_metrics(*_: Any) -> None:
            points = collector.snapshot()
            if is_hassio(hass):
                try:
                    points.update(await supervisor_metrics(hass))
                except SupervisorError as err:
                    _LOGGER.debug("Could not read Supervisor stats: %s", err)
            status = await exporter.post("/v1/metrics", metrics_payload(resource, points))
            if status is not None and status < 400:
                exporter.metrics_exported += sum(len(p) for p in points.values())
            exporter.notify()

        entry.async_on_unload(async_at_started(hass, export_metrics))
        entry.async_on_unload(
            async_track_time_interval(hass, export_metrics, METRICS_INTERVAL, name="bindplane metrics")
        )

    coordinator = None
    if entry.data.get(CONF_API_KEY) and entry.data.get(CONF_AGENT_ID):
        client = BindplaneClient(
            async_get_clientsession(hass),
            entry.data[CONF_API_URL],
            entry.data[CONF_API_KEY],
            entry.data.get(CONF_PROJECT_ID),
        )

        async def update() -> dict[str, Any] | None:
            try:
                return await client.collector(entry.data[CONF_AGENT_ID])
            except BindplaneAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except BindplaneError as err:
                raise UpdateFailed(str(err)) from err

        coordinator = DataUpdateCoordinator(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_method=update, update_interval=timedelta(seconds=60)
        )
        await coordinator.async_refresh()

    entry.runtime_data = BindplaneData(exporter, coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: BindplaneConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BindplaneConfigEntry) -> bool:
    """Flush what's buffered and detach."""
    await entry.runtime_data.exporter.flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
