"""Tests for the OTLP exporter."""

import json
import logging
import sys

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.trace import trace_id_cv
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.exporter import (
    Exporter,
    OtlpHandler,
    attr,
    event_record,
    flatten,
    log_record,
    payload,
    span_id,
)

URL = "http://collector:4318"


def make_record(msg="boom", level=logging.ERROR, exc=False, name="homeassistant.components.zha"):
    exc_info = None
    if exc:
        try:
            raise ValueError("bad value")
        except ValueError:
            exc_info = sys.exc_info()
    return logging.LogRecord(name, level, "/x/zha.py", 42, msg, None, exc_info, "async_update")


def bodies(data):
    return [r["body"]["stringValue"] for s in json.loads(data)["resourceLogs"][0]["scopeLogs"] for r in s["logRecords"]]


def attrs(rec):
    return {a["key"]: next(iter(a["value"].values())) for a in rec["attributes"]}


def test_log_record_maps_fields():
    scope, rec = log_record(make_record(exc=True))
    assert scope == "homeassistant.components.zha"
    assert rec["severityNumber"] == 17 and rec["severityText"] == "ERROR"
    assert rec["body"] == {"stringValue": "boom"}
    a = attrs(rec)
    assert a["code.line.number"] == "42"
    assert a["code.function.name"] == "async_update"
    assert a["exception.type"] == "ValueError"
    assert "Traceback" in a["exception.stacktrace"]


def test_payload_groups_by_scope():
    recs = [log_record(make_record(name=n)) for n in ("a", "b", "a")]
    out = payload([attr("service.name", "home-assistant")], recs)
    scopes = out["resourceLogs"][0]["scopeLogs"]
    assert [(s["scope"]["name"], len(s["logRecords"])) for s in scopes] == [("a", 2), ("b", 1)]


def test_flatten():
    assert flatten("e", {"a": {"b": 1}, "c": [1], "d": None, "f": True}) == [
        attr("e.a.b", 1),
        attr("e.c", "[1]"),
        attr("e.f", True),
    ]


def test_handler_excludes_and_drops_oldest(monkeypatch):
    monkeypatch.setattr("custom_components.bindplane.exporter.BUFFER_SIZE", 2)
    handler = OtlpHandler(logging.INFO, ["homeassistant.components.recorder"])
    handler.buffer = handler.buffer.__class__(maxlen=2)
    handler.handle(make_record(name="homeassistant.components.recorder.core"))
    handler.handle(make_record(name="custom_components.bindplane"))
    assert not handler.buffer
    for msg in ("1", "2", "3"):
        handler.handle(make_record(msg))
    assert [r["body"]["stringValue"] for _, r in handler.buffer] == ["2", "3"]
    assert handler.dropped == 1


async def test_flush_retries_then_succeeds(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    handler = OtlpHandler(logging.INFO, [])
    exporter = Exporter(hass, URL, [], handler)
    handler.handle(make_record("first"))

    aioclient_mock.post(f"{URL}/v1/logs", status=503)
    await exporter.flush()
    assert exporter.status == "error" and exporter.exported == 0
    assert len(exporter._pending) == 1

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/logs", status=200, json={})
    handler.handle(make_record("second"))
    await exporter.flush()
    assert exporter.status == "ok" and exporter.exported == 2
    assert [bodies(c[2]) for c in aioclient_mock.mock_calls] == [["first"], ["second"]]


async def test_flush_drops_rejected_batch(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    handler = OtlpHandler(logging.INFO, [])
    exporter = Exporter(hass, URL, [], handler)
    handler.handle(make_record())
    aioclient_mock.post(f"{URL}/v1/logs", status=400, text="bad")
    await exporter.flush()
    assert exporter.dropped == 1 and not exporter._pending and exporter.last_error == "HTTP 400: bad"


async def test_state_changed_event_record(hass: HomeAssistant):
    area = ar.async_get(hass).async_create("Kitchen")
    config_entry = MockConfigEntry(domain="tplink")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("tplink", "1")}, name="Plug", manufacturer="TP-Link"
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    er.async_get(hass).async_get_or_create(
        "switch", "tplink", "1", suggested_object_id="plug", device_id=device.id, config_entry=config_entry
    )
    hass.states.async_set("switch.plug", "off", {"friendly_name": "Plug", "power": 1})
    old = hass.states.get("switch.plug")
    hass.states.async_set("switch.plug", "on", {"friendly_name": "Plug", "power": 5})
    event = Event(
        EVENT_STATE_CHANGED,
        {"entity_id": "switch.plug", "old_state": old, "new_state": hass.states.get("switch.plug")},
        context=Context(user_id="u1"),
    )
    scope, record = event_record(event, hass)
    a = attrs(record)
    assert scope == "homeassistant.events" and record["eventName"] == "state_changed"
    assert record["body"]["stringValue"] == "switch.plug: off → on"
    assert a["homeassistant.entity.id"] == "switch.plug"
    assert a["homeassistant.entity.domain"] == "switch" and a["homeassistant.entity.object_id"] == "plug"
    assert a["homeassistant.entity.platform"] == "tplink"
    assert a["homeassistant.device.name"] == "Plug" and a["homeassistant.device.manufacturer"] == "TP-Link"
    assert a["homeassistant.area.name"] == "Kitchen"
    assert a["homeassistant.state.old"] == "off" and a["homeassistant.state.new"] == "on"
    assert a["homeassistant.state.attributes.power"] == "5"
    assert a["homeassistant.state.old_attributes.power"] == "1"
    assert "homeassistant.state.old_attributes.friendly_name" not in a
    assert a["user.id"] == "u1"
    assert not [k for k in a if k.startswith("homeassistant.event.data")]


def test_log_record_links_to_running_trace():
    assert "traceId" not in log_record(make_record())[1]
    token = trace_id_cv.set(("automation.porch", "a" * 32))
    try:
        _, record = log_record(make_record())
    finally:
        trace_id_cv.reset(token)
    assert record["traceId"] == "a" * 32 and record["spanId"] == span_id("a" * 32, "")
