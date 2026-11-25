"""Shared JSON log format with trace fields (#72)."""

import json
import logging

from app.shared.contracts import EventEnvelope
from app.shared.logging import JsonFormatter, trace


def test_log_line_carries_component_and_trace_fields(example):
    envelope = EventEnvelope.model_validate(example("city-state-updated-event.json"))
    record = logging.LogRecord("c07", logging.INFO, __file__, 1, "handled", None, None)
    for key, value in trace(envelope).items():
        setattr(record, key, value)

    line = json.loads(JsonFormatter("C07").format(record))

    assert line["component"] == "C07"
    assert line["message"] == "handled"
    assert line["correlationId"] == str(envelope.correlation_id)
    assert line["eventId"] == str(envelope.event_id)
    assert line["scope"] == "simulation"
    assert line["runId"] == str(envelope.run_id)


def test_fields_that_are_not_set_are_left_out():
    record = logging.LogRecord("c06", logging.INFO, __file__, 1, "started", None, None)

    line = json.loads(JsonFormatter("C06").format(record))

    assert "correlationId" not in line


def test_revised_plan_trace_identifies_the_plan_and_its_trigger(example):
    event = EventEnvelope.model_validate(example("plan-ready-revision-event.json"))
    record = logging.makeLogRecord(trace(event))
    line = json.loads(JsonFormatter("C05").format(record))
    assert line["planId"] == str(event.payload["planId"])
    assert line["causationId"] == str(event.causation_id)


def test_a_line_may_name_its_own_component():
    record = logging.LogRecord("c09", logging.INFO, __file__, 1, "query", None, None)
    record.component = "C09"

    assert json.loads(JsonFormatter("API").format(record))["component"] == "C09"
