"""Boundary tests for the proposed UC11/UC12 contract, not runtime evidence."""

import copy
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.components.c11_c12_contracts import (
    AnalysisScope,
    CannotReplay,
    Coverage,
    Indicator,
    KPISet,
    RecordingRecord,
    ReplayControl,
    ReplayFrame,
    ReplayRequest,
    RunManifest,
    TimeRange,
)

EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "contracts/examples/c11-c12.json"
EXAMPLES = json.loads(EXAMPLE_PATH.read_text())


class ContractTests(unittest.TestCase):
    def test_all_published_examples_validate(self):
        models = {
            "partialKpis": KPISet,
            "staleKpis": KPISet,
            "manifest": RunManifest,
            "record": RecordingRecord,
            "replayRequest": ReplayRequest,
            "pausedFrame": ReplayFrame,
            "cannotReplay": CannotReplay,
            "seek": ReplayControl,
        }
        for key, model in models.items():
            with self.subTest(key=key):
                model.model_validate(EXAMPLES[key])

    def test_missing_is_not_zero(self):
        item = EXAMPLES["partialKpis"]["indicators"][1]
        self.assertIsNone(Indicator.model_validate(item).value)
        with self.assertRaises(ValidationError):
            Indicator.model_validate({**item, "value": 0})

    def test_measured_zero_is_valid(self):
        item = EXAMPLES["partialKpis"]["indicators"][0]
        self.assertEqual(Indicator.model_validate({**item, "value": 0}).value, 0)

    def test_no_nan_or_infinite_indicator(self):
        item = EXAMPLES["partialKpis"]["indicators"][0]
        for value in [float("nan"), float("inf"), -float("inf")]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Indicator.model_validate({**item, "value": value})

    def test_estimate_requires_basis(self):
        item = EXAMPLES["partialKpis"]["indicators"][0]
        with self.assertRaises(ValidationError):
            Indicator.model_validate({**item, "estimated": True})

    def test_coverage_cannot_exceed_population(self):
        with self.assertRaises(ValidationError):
            Coverage(eligible=2, observed=3)

    def test_incomplete_coverage_cannot_claim_available(self):
        item = EXAMPLES["partialKpis"]["indicators"][0]
        with self.assertRaises(ValidationError):
            Indicator.model_validate(
                {**item, "coverage": {"eligible": 3, "observed": 2}}
            )

    def test_no_observations_cannot_produce_value(self):
        item = EXAMPLES["partialKpis"]["indicators"][0]
        with self.assertRaises(ValidationError):
            Indicator.model_validate(
                {**item, "coverage": {"eligible": 0, "observed": 0}}
            )

    def test_stale_preserves_original_values_and_timestamp(self):
        result = KPISet.model_validate(EXAMPLES["staleKpis"])
        previous = KPISet.model_validate(EXAMPLES["partialKpis"])
        self.assertEqual(result.indicators, previous.indicators)
        with self.assertRaises(ValidationError):
            KPISet.model_validate({**EXAMPLES["staleKpis"], "failure": None})

    def test_partial_result_cannot_claim_current(self):
        with self.assertRaises(ValidationError):
            KPISet.model_validate({**EXAMPLES["partialKpis"], "status": "current"})

    def test_duplicate_metric_ids_rejected(self):
        data = copy.deepcopy(EXAMPLES["partialKpis"])
        data["indicators"].append(data["indicators"][0])
        with self.assertRaises(ValidationError):
            KPISet.model_validate(data)

    def test_utc_and_time_range_validation(self):
        for start, end in [
            ("2026-11-17T10:00:00", "2026-11-17T11:00:00Z"),
            ("2026-11-17T10:00:00+01:00", "2026-11-17T11:00:00Z"),
            ("2026-11-17T11:00:00Z", "2026-11-17T10:00:00Z"),
            ("2026-11-17T11:00:00Z", "2026-11-17T11:00:00Z"),
        ]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValidationError):
                    TimeRange(start=start, end=end)

    def test_run_scope_cannot_leak_into_live(self):
        scope = EXAMPLES["partialKpis"]["scope"]
        with self.assertRaises(ValidationError):
            AnalysisScope.model_validate({**scope, "scope": "live"})
        with self.assertRaises(ValidationError):
            AnalysisScope.model_validate({**scope, "runId": None})

    def test_record_requires_matching_simulation_run(self):
        for event_change in [
            {"scope": "live", "runId": None},
            {"runId": "99999999-9999-4999-8999-999999999999"},
        ]:
            data = copy.deepcopy(EXAMPLES["record"])
            data["event"].update(event_change)
            with self.subTest(change=event_change):
                with self.assertRaises(ValidationError):
                    RecordingRecord.model_validate(data)

    def test_replay_speed_bounded_and_finite(self):
        for speed in [0, -1, 17, float("nan"), float("inf")]:
            with self.subTest(speed=speed), self.assertRaises(ValidationError):
                ReplayRequest.model_validate(
                    {**EXAMPLES["replayRequest"], "speed": speed}
                )

    def test_seek_requires_target_and_other_commands_forbid_it(self):
        data = EXAMPLES["seek"]
        for changes in [{"targetCursor": None}, {"command": "play"}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ReplayControl.model_validate({**data, **changes})

    def test_negative_and_boolean_cursors_rejected(self):
        for cursor in [-1, True]:
            with self.subTest(cursor=cursor), self.assertRaises(ValidationError):
                ReplayControl.model_validate(
                    {**EXAMPLES["seek"], "expectedCursor": cursor}
                )

    def test_interruption_requires_paused_status(self):
        with self.assertRaises(ValidationError):
            ReplayFrame.model_validate(
                {**EXAMPLES["pausedFrame"], "status": "playing"}
            )

    def test_unknown_fields_fail_loudly(self):
        with self.assertRaises(ValidationError):
            ReplayFrame.model_validate(
                {**EXAMPLES["pausedFrame"], "publishLiveCommand": True}
            )

    def test_future_schema_version_rejected(self):
        with self.assertRaises(ValidationError):
            KPISet.model_validate({**EXAMPLES["partialKpis"], "schemaVersion": 2})


if __name__ == "__main__":
    unittest.main()
