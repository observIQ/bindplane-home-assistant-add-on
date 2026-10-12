"""Tests for entity state metrics."""

from datetime import timedelta
import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.const import CONF_METRICS_EXCLUDE, CONF_OTLP_URL, DOMAIN
from custom_components.bindplane.exporter import attr
from custom_components.bindplane.metrics import snapshot

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
    hass.states.async_set("light.kitchen", "off")
    hass.states.async_set("lock.front", "locked")


async def test_snapshot(hass: HomeAssistant):
    area = ar.async_get(hass).async_create("Den")
    er.async_get(hass).async_get_or_create("sensor", "demo", "t1", suggested_object_id="den_temp")
    er.async_get(hass).async_update_entity("sensor.den_temp", area_id=area.id)
    set_states(hass)

    metrics = snapshot(hass, {"sensor.hidden"})

    assert set(metrics) == {
        ("homeassistant.sensor.temperature", "°C"),
        ("homeassistant.sensor.power", "W"),
        ("homeassistant.binary_sensor.on", ""),
        ("homeassistant.light.on", ""),
    }
    [temp] = metrics[("homeassistant.sensor.temperature", "°C")]
    assert temp["asDouble"] == 21.5
    assert temp["attributes"] == [
        attr("entity_id", "sensor.den_temp"),
        attr("friendly_name", "Den"),
        attr("area", "Den"),
    ]
    assert metrics[("homeassistant.binary_sensor.on", "")][0]["asDouble"] == 1.0
    assert metrics[("homeassistant.light.on", "")][0]["asDouble"] == 0.0


async def test_metrics_exported_every_minute(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/metrics", json={})
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    set_states(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Bindplane", data={CONF_OTLP_URL: URL}, options={CONF_METRICS_EXCLUDE: ["sensor.power"]}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    [call] = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/metrics")]
    body = json.loads(call[2])
    names = {m["name"] for m in body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    assert "homeassistant.sensor.temperature" in names and "homeassistant.sensor.power" not in names
    assert int(hass.states.get("sensor.bindplane_metric_data_points_exported").state) >= 3


async def test_metrics_can_be_disabled(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    set_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_OTLP_URL: URL}, options={"metrics": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/metrics")]
