"""Automation runs are exported as OTLP traces."""

from datetime import timedelta
import json

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.const import CONF_OTLP_URL, DOMAIN

URL = "http://collector:4318"
AUTOMATION = {
    "id": "porch",
    "alias": "Porch light",
    "triggers": [{"trigger": "event", "event_type": "motion"}],
    "actions": [
        {"variables": {"bright": 1}},
        {
            "choose": [
                {
                    "conditions": [{"condition": "template", "value_template": "{{ bright == 1 }}"}],
                    "sequence": [{"event": "porch_on"}],
                }
            ]
        },
    ],
}


def attrs(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


async def test_automation_run_exported(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    for path in ("logs", "metrics", "traces"):
        aioclient_mock.post(f"{URL}/v1/{path}", json={})
    assert await async_setup_component(hass, "automation", {"automation": [AUTOMATION]})
    entry = MockConfigEntry(domain=DOMAIN, title="Bindplane", data={CONF_OTLP_URL: URL}, options={"metrics": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    hass.bus.async_fire("motion")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()

    [call] = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/traces")]
    spans = json.loads(call[2])["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = spans[0]
    assert root["name"] == "automation: Porch light"
    assert len(root["traceId"]) == 32 and all(s["traceId"] == root["traceId"] for s in spans)
    assert "status" not in root
    assert attrs(root)["entity_id"] == "automation.porch_light"
    assert attrs(root)["homeassistant.script_execution"] == "finished"
    assert "event 'motion'" in attrs(root)["homeassistant.trigger"]

    by_name = {s["name"]: s for s in spans[1:]}
    assert {"trigger/0", "action/0", "action/1", "action/1/choose/0/sequence/0"} <= set(by_name)
    assert by_name["action/0"]["parentSpanId"] == root["spanId"]
    assert by_name["action/1/choose/0"]["parentSpanId"] == by_name["action/1"]["spanId"]
    assert by_name["action/1/choose/0/sequence/0"]["parentSpanId"] == by_name["action/1/choose/0"]["spanId"]
    assert all(int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"]) for s in spans)
    assert int(hass.states.get("sensor.bindplane_trace_spans_exported").state) == len(spans)

    aioclient_mock.clear_requests()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/traces")]


async def test_failed_run_marked_as_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    for path in ("logs", "metrics", "traces"):
        aioclient_mock.post(f"{URL}/v1/{path}", json={})
    broken = {**AUTOMATION, "id": "broken", "alias": "Broken", "actions": [{"action": "nope.missing"}]}
    assert await async_setup_component(hass, "automation", {"automation": [broken]})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_OTLP_URL: URL}, options={"metrics": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    hass.bus.async_fire("motion")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()

    [call] = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v1/traces")]
    spans = json.loads(call[2])["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0]["status"]["code"] == 2
    assert next(s for s in spans if s["name"] == "action/0")["status"]["code"] == 2
