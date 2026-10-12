"""Tests for setting up the integration."""

import json
import logging

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.const import CONF_EVENTS, CONF_OTLP_URL, DOMAIN

URL = "http://collector:4318"


def sent(aioclient_mock):
    return [
        (s["scope"]["name"], r)
        for c in aioclient_mock.mock_calls
        for s in json.loads(c[2])["resourceLogs"][0]["scopeLogs"]
        for r in s["logRecords"]
    ]


async def test_logs_and_events_are_exported(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    entry = MockConfigEntry(
        domain=DOMAIN, title="Bindplane Collector", data={CONF_OTLP_URL: URL}, options={CONF_EVENTS: ["custom_event"]}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    logging.getLogger("homeassistant.components.demo").warning("Hello %s", "world")
    logging.getLogger("homeassistant.components.demo").debug("too quiet")
    hass.bus.async_fire("custom_event", {"answer": 42})
    await hass.services.async_call(DOMAIN, "flush", blocking=True)

    records = sent(aioclient_mock)
    by_body = {r["body"]["stringValue"]: (scope, r) for scope, r in records}
    assert "too quiet" not in by_body
    assert by_body["Hello world"][0] == "homeassistant.components.demo"
    scope, event = by_body["custom_event"]
    assert scope == "homeassistant.events"
    assert {"key": "event.data.answer", "value": {"intValue": "42"}} in event["attributes"]
    resource = json.loads(aioclient_mock.mock_calls[0][2])["resourceLogs"][0]["resource"]["attributes"]
    assert {"key": "service.name", "value": {"stringValue": "home-assistant"}} in resource

    assert hass.states.get("sensor.bindplane_collector_export_status").state == "ok"
    assert int(hass.states.get("sensor.bindplane_collector_records_exported").state) >= 2

    await hass.services.async_call(DOMAIN, "send_test", {"message": "ping"}, blocking=True)
    assert "ping" in {r["body"]["stringValue"] for _, r in sent(aioclient_mock)}

    assert await hass.config_entries.async_unload(entry.entry_id)
    handler_count = len(logging.getLogger().handlers)
    logging.getLogger("homeassistant.components.demo").warning("after unload")
    assert len(logging.getLogger().handlers) == handler_count
    assert "after unload" not in {r["body"]["stringValue"] for _, r in sent(aioclient_mock)}
