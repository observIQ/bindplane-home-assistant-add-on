"""Add-on, Supervisor and host logs are followed through the Supervisor API."""

from datetime import UTC, datetime, timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

from aiohasupervisor.models import AddonState

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.const import CONF_OTLP_URL, DOMAIN
from custom_components.bindplane.supervisor_logs import log_record, parse_line

URL = "http://collector:4318"


def ts(offset: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def attrs(record):
    return {a["key"]: next(iter(a["value"].values())) for a in record["attributes"]}


def test_parse_and_convert():
    entry = parse_line(
        "2026-10-12 17:39:08.123 homeassistant addon_core_mosquitto[42]: 1760290748: Error: socket closed"
    )
    assert entry.identifier == "addon_core_mosquitto" and entry.pid == "42"
    entry.lines.append("  second line")
    assert parse_line("  second line") is None
    scope, record = log_record(entry, "addon.core_mosquitto", [])
    assert scope == "homeassistant.supervisor.addon.core_mosquitto"
    assert record["body"]["stringValue"] == "1760290748: Error: socket closed\n  second line"
    assert record["severityText"] == "ERROR"
    assert record["timeUnixNano"] == str(int(datetime(2026, 10, 12, 17, 39, 8, 123000, tzinfo=UTC).timestamp() * 1e9))
    assert attrs(record)["process.pid"] == "42"
    _, plain = log_record(parse_line("2026-10-12 17:39:08.123 host kernel: eth0 link up"), "host", [])
    assert "severityText" not in plain and "process.pid" not in attrs(plain)


async def test_streams_exported(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch):
    monkeypatch.setenv("SUPERVISOR", "supervisor")
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    aioclient_mock.get(
        "http://supervisor/addons/core_mosquitto/logs/follow",
        text="\n".join(
            [
                f"{ts(-60)} ha addon_core_mosquitto[7]: replayed before setup",
                f"{ts(5)} ha addon_core_mosquitto[7]: [WARN] client timeout",
                "Traceback line",
                f"{ts(6)} ha addon_core_mosquitto[7]: info: connected",
            ]
        ),
    )
    aioclient_mock.get(
        "http://supervisor/supervisor/logs/follow", text=f"{ts(5)} ha hassio_supervisor[1]: Supervisor ok"
    )
    aioclient_mock.get("http://supervisor/host/logs/follow", text=f"{ts(5)} ha kernel: eth0 up")
    mosquitto = MagicMock(slug="core_mosquitto", state=AddonState.STARTED)
    mosquitto.name = "Mosquitto broker"
    client = MagicMock()
    client.addons.list = AsyncMock(return_value=[mosquitto, MagicMock(slug="stopped_one", state=AddonState.STOPPED)])
    with (
        patch("custom_components.bindplane.is_hassio", return_value=True),
        patch("custom_components.bindplane.supervisor_logs.get_supervisor_client", return_value=client),
        patch("custom_components.bindplane.supervisor_metrics", AsyncMock(return_value={})),
        patch("custom_components.bindplane.supervisor_logs.RETRY_AFTER", 3600),
    ):
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_OTLP_URL: URL}, options={"metrics": False, "events": []})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(DOMAIN, "flush", blocking=True)

        records = [
            (s["scope"]["name"], r)
            for c in aioclient_mock.mock_calls
            if str(c[1]).endswith("/v1/logs")
            for s in json.loads(c[2])["resourceLogs"][0]["scopeLogs"]
            for r in s["logRecords"]
            if s["scope"]["name"].startswith("homeassistant.supervisor")
        ]
        bodies = {r["body"]["stringValue"]: (scope, r) for scope, r in records}
        assert "replayed before setup" not in bodies
        scope, warn = bodies["[WARN] client timeout\nTraceback line"]
        assert scope == "homeassistant.supervisor.addon.core_mosquitto"
        assert warn["severityText"] == "WARNING"
        assert attrs(warn)["homeassistant.addon.name"] == "Mosquitto broker"
        assert attrs(warn)["container.name"] == "addon_core_mosquitto"
        assert bodies["info: connected"][1]["severityText"] == "INFO"
        assert bodies["Supervisor ok"][0] == "homeassistant.supervisor.supervisor"
        assert bodies["eth0 up"][0] == "homeassistant.supervisor.host"
        assert not [c for c in aioclient_mock.mock_calls if "stopped_one" in str(c[1])]
        assert await hass.config_entries.async_unload(entry.entry_id)
