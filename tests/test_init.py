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
        if str(c[1]).endswith("/v1/logs")
        for s in json.loads(c[2])["resourceLogs"][0]["scopeLogs"]
        for r in s["logRecords"]
    ]


async def test_logs_and_events_are_exported(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    aioclient_mock.post(f"{URL}/v1/metrics", json={})
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
    assert {"key": "homeassistant.event.data.answer", "value": {"intValue": "42"}} in event["attributes"]
    logs_call = next(c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/logs"))
    resource = json.loads(logs_call[2])["resourceLogs"][0]["resource"]["attributes"]
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


async def test_info_logs_captured_without_noisier_ha_log(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    root.setLevel(logging.WARNING)
    try:
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_OTLP_URL: URL}, options={"metrics": False, "events": []})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert root.level == logging.INFO
        assert other.level == logging.WARNING

        logging.getLogger("homeassistant.components.demo").info("now visible")
        await hass.services.async_call(DOMAIN, "flush", blocking=True)
        assert "now visible" in {r["body"]["stringValue"] for _, r in sent(aioclient_mock)}

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert root.level == logging.WARNING
        assert other.level == logging.NOTSET
    finally:
        root.removeHandler(other)


async def test_all_events_by_default_except_state_changed(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_OTLP_URL: URL}, options={"metrics": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    hass.bus.async_fire("my_custom_event", {"x": 1})
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "flush", blocking=True)

    events = {r["eventName"] for scope, r in sent(aioclient_mock) if scope == "homeassistant.events"}
    assert "my_custom_event" in events
    assert "state_changed" not in events


def test_bindplane_links():
    from custom_components.bindplane.api import bindplane_link

    base = {"api_url": "https://app.bindplane.com"}
    assert bindplane_link({}) is None
    assert bindplane_link(base) == "https://app.bindplane.com/"
    assert bindplane_link({**base, "agent_id": "a1"}) == "https://app.bindplane.com/collectors/a1"
    assert (
        bindplane_link({**base, "agent_id": "a1", "configuration": "home assistant", "project_id": "p1"})
        == "https://app.bindplane.com/p/p1/configurations/home%20assistant"
    )


async def test_revoked_api_key_starts_reauth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{URL}/v1/logs", json={})
    aioclient_mock.post(f"{URL}/v1/metrics", json={})
    aioclient_mock.get("https://app.bindplane.com/v1/collectors/agent-1", status=401)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_OTLP_URL: URL, "api_url": "https://app.bindplane.com", "api_key": "old", "agent_id": "agent-1"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert flows[0]["step_id"] == "api_key"
