"""Tests for entity state and Supervisor metrics."""

from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

from aiohasupervisor.models import AddonState

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.const import CONF_METRICS_EXCLUDE, CONF_OTLP_URL, DOMAIN
from custom_components.bindplane.exporter import attr
from custom_components.bindplane.metrics import GAUGE, SUM, MetricsCollector, supervisor_metrics

URL = "http://collector:4318"


def set_states(hass):
    hass.states.async_set(
        "sensor.den_temp", "21.5", {"device_class": "temperature", "unit_of_measurement": "°C", "friendly_name": "Den"}
    )
    hass.states.async_set("sensor.power", "120", {"device_class": "power", "unit_of_measurement": "W"})
    hass.states.async_set("sensor.weather", "sunny")
    hass.states.async_set("sensor.broken", "nan")
    hass.states.async_set("sensor.hidden", "1")
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("light.kitchen", "on", {"brightness": 200, "supported_features": 4, "effect": "none"})
    hass.states.async_set("climate.hall", "heat", {"current_temperature": 19.5, "temperature": 21})
    hass.states.async_set("lock.front", "unavailable")


def values(metrics, name, kind=GAUGE):
    return {
        next(a["value"]["stringValue"] for a in p["attributes"] if a["key"] == "homeassistant.entity.id"): p.get(
            "asDouble", p.get("asInt")
        )
        for (n, _, k), points in metrics.items()
        if n == name and k == kind
        for p in points
    }


async def test_snapshot(hass: HomeAssistant):
    area = ar.async_get(hass).async_create("Den")
    er.async_get(hass).async_get_or_create("sensor", "demo", "t1", suggested_object_id="den_temp")
    er.async_get(hass).async_update_entity("sensor.den_temp", area_id=area.id)
    set_states(hass)
    collector = MetricsCollector(hass, {"sensor.hidden"})
    hass.bus.async_listen("state_changed", collector.async_count_change)
    hass.states.async_set("sensor.power", "130", {"device_class": "power", "unit_of_measurement": "W"})
    await hass.async_block_till_done()

    metrics = collector.snapshot()

    [temp] = metrics[("homeassistant.sensor.temperature", "°C", GAUGE)]
    assert temp["asDouble"] == 21.5
    assert attr("homeassistant.area.name", "Den") in temp["attributes"]
    assert values(metrics, "homeassistant.sensor.power") == {"sensor.power": 130.0}
    assert values(metrics, "homeassistant.binary_sensor.on") == {"binary_sensor.door": 1.0}
    assert values(metrics, "homeassistant.light.attribute.brightness") == {"light.kitchen": 200.0}
    assert "homeassistant.light.attribute.supported_features" not in {n for n, _, _ in metrics}
    assert values(metrics, "homeassistant.climate.attribute.current_temperature") == {"climate.hall": 19.5}
    states = {p["attributes"][-1]["value"]["stringValue"] for p in metrics[("homeassistant.entity.state", "", GAUGE)]}
    assert states == {"sunny", "heat"}
    available = values(metrics, "homeassistant.entity.available")
    assert available["lock.front"] == 0.0 and available["sensor.den_temp"] == 1.0
    assert "sensor.hidden" not in available
    assert values(metrics, "homeassistant.entity.state_changes", SUM) == {"sensor.power": "1"}


async def test_supervisor_metrics(hass: HomeAssistant):
    stats = MagicMock(
        cpu_percent=12.5,
        memory_usage=1000,
        memory_limit=4000,
        memory_percent=25.0,
        network_rx=1,
        network_tx=2,
        blk_read=3,
        blk_write=4,
    )
    client = MagicMock()
    client.homeassistant.stats = AsyncMock(return_value=stats)
    client.supervisor.stats = AsyncMock(return_value=stats)
    client.addons.list = AsyncMock(
        return_value=[
            MagicMock(slug="core_mosquitto", state=AddonState.STARTED),
            MagicMock(slug="core_stopped", state=AddonState.STOPPED),
        ]
    )
    client.addons.addon_stats = AsyncMock(return_value=stats)
    client.host.info = AsyncMock(return_value=MagicMock(disk_used=10.0, disk_free=22.0))

    with patch("custom_components.bindplane.metrics.get_supervisor_client", return_value=client):
        metrics = await supervisor_metrics(hass)

    cpu = metrics[("container.cpu.utilization", "1", GAUGE)]
    names = [next(a["value"]["stringValue"] for a in p["attributes"] if a["key"] == "container.name") for p in cpu]
    assert names == ["homeassistant", "hassio_supervisor", "addon_core_mosquitto"]
    assert cpu[0]["asDouble"] == 0.125
    assert len(metrics[("container.network.io", "By", SUM)]) == 6
    assert [p["asDouble"] for p in metrics[("system.filesystem.usage", "By", GAUGE)]] == [10e9, 22e9]


async def test_metrics_exported_every_minute(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/metrics", json={})
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    set_states(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Bindplane", data={CONF_OTLP_URL: URL}, options={CONF_METRICS_EXCLUDE: ["sensor.power"]}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    calls = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/metrics")]
    assert len(calls) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    calls = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/metrics")]
    assert len(calls) == 2
    metrics = json.loads(calls[-1][2])["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    names = {m["name"] for m in metrics}
    assert "homeassistant.sensor.temperature" in names and "homeassistant.sensor.power" not in names
    assert int(hass.states.get("sensor.bindplane_metric_data_points_exported").state) > 10


async def test_metrics_can_be_disabled(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_OTLP_URL: URL}, options={"metrics": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/metrics")]
