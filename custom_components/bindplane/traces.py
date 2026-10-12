"""Automation and script run traces as OTLP spans."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.trace.models import RestoredTrace
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .exporter import attr, entity_attrs, flatten, span_id as _span_id

TRACE_DATA = "trace"
STATUS_ERROR = 2
FAILED = {"error", "failed_single", "failed_max_runs", "aborted"}


def _ns(ts: datetime) -> str:
    return str(int(ts.timestamp() * 1e9))


def run_spans(run: dict[str, Any], entity: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Convert a finished automation or script trace into spans."""
    run_id = run["run_id"]
    config = run.get("config") or {}
    start, finish = run["timestamp"]["start"], run["timestamp"]["finish"]
    root_id = _span_id(run_id, "")
    execution = run.get("script_execution")

    root: dict[str, Any] = {
        "traceId": run_id,
        "spanId": root_id,
        "name": f"{run['domain']}: {config.get('alias') or run['item_id']}",
        "kind": 1,
        "startTimeUnixNano": _ns(start),
        "endTimeUnixNano": _ns(finish),
        "attributes": [
            *flatten(
                "homeassistant",
                {
                    "trace.run_id": run_id,
                    "trace.domain": run["domain"],
                    "trace.item_id": run["item_id"],
                    "trace.alias": config.get("alias"),
                    "trace.script_execution": execution,
                    "trace.last_step": run.get("last_step"),
                    "context.id": run["context"].id,
                    "context.parent_id": run["context"].parent_id,
                },
            ),
            *([attr("user.id", run["context"].user_id)] if run["context"].user_id else []),
            *(entity or []),
        ],
    }
    for element in run.get("trace", {}).get("trigger/0", []):
        if description := ((element.get("changed_variables") or {}).get("trigger") or {}).get("description"):
            root["attributes"].append(attr("homeassistant.trace.trigger", description))
    if run.get("error") or execution in FAILED:
        root["status"] = {"code": STATUS_ERROR, "message": run.get("error") or execution}
        root["attributes"].append(attr("error.type", execution or "error"))

    steps = sorted(
        (
            (element["timestamp"], path, element)
            for path, elements in run.get("trace", {}).items()
            for element in elements
        ),
        key=lambda step: step[0],
    )
    spans = [root]
    latest: dict[str, str] = {}
    for index, (ts, path, element) in enumerate(steps):
        end = steps[index + 1][0] if index + 1 < len(steps) else finish
        parent = next(
            (latest[p] for p in sorted(latest, key=len, reverse=True) if path.startswith(f"{p}/")),
            root_id,
        )
        span_id = _span_id(run_id, f"{path}#{index}")
        latest[path] = span_id
        span: dict[str, Any] = {
            "traceId": run_id,
            "spanId": span_id,
            "parentSpanId": parent,
            "name": path,
            "kind": 1,
            "startTimeUnixNano": _ns(ts),
            "endTimeUnixNano": _ns(max(end, ts)),
            "attributes": [
                attr("homeassistant.step.path", path),
                *flatten("homeassistant.step.result", element.get("result") or {}),
                *flatten("homeassistant.step.variables", element.get("changed_variables") or {}, depth=2),
            ],
        }
        if child := element.get("child_id"):
            span["attributes"].append(attr("homeassistant.step.child_run_id", child["run_id"]))
            span["links"] = [{"traceId": child["run_id"], "spanId": _span_id(child["run_id"], "")}]
        if error := element.get("error"):
            span["status"] = {"code": STATUS_ERROR, "message": error}
            span["attributes"].append(attr("error.type", "error"))
        spans.append(span)
    return spans


class TraceCollector:
    """Finds automation and script runs that finished since the last look."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._seen: set[str] = {run["run_id"] for run in self._finished()}

    def _finished(self) -> list[dict[str, Any]]:
        buckets = self._hass.data.get(TRACE_DATA) or {}
        runs = (
            trace.as_extended_dict()
            for bucket in buckets.values()
            for trace in getattr(bucket, "runs", bucket).values()
            if not isinstance(trace, RestoredTrace)
        )
        return [run for run in runs if run["state"] == "stopped" and run["timestamp"]["finish"]]

    def collect(self) -> list[dict[str, Any]]:
        """Return spans for newly finished runs."""
        finished = self._finished()
        new = [run for run in finished if run["run_id"] not in self._seen]
        self._seen = {run["run_id"] for run in finished}
        entities = er.async_get(self._hass)
        spans = []
        for run in new:
            entity_id = entities.async_get_entity_id(run["domain"], run["domain"], run["item_id"])
            spans += run_spans(run, entity_attrs(self._hass, entity_id) if entity_id else None)
        return spans


def traces_payload(resource: list[dict[str, Any]], spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an OTLP ExportTraceServiceRequest."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource},
                "scopeSpans": [{"scope": {"name": "homeassistant.trace"}, "spans": spans}],
            }
        ]
    }
