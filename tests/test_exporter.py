"""Tests for the OTLP exporter."""

import json
import logging
import sys

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.exporter import Exporter, OtlpHandler, attr, flatten, log_record, payload

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
