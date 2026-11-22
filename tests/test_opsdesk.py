import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from opsdesk.evidence import EvidenceBook, build_report
from opsdesk.ingest import Ingestor, load_sources
from opsdesk.registry import load_registry
from opsdesk.units import convert, parse_time, to_decimal

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "fixtures" / "registry.json"
SOURCES = ROOT / "fixtures" / "sources.json"


def build_fixture(sources, registry=REGISTRY):
    """把来源列表写成临时目录，返回构建好的报告字典。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sources.json"
        path.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
        return build_report(registry, path, as_of="2026-12-18T06:30:00+08:00")


def envelope(source_id="t-1", kind="telemetry", collected="2026-12-18T05:20:00+08:00", **payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected,
        "payload": payload,
    }


def reading(**overrides):
    payload = {
        "asset_id": "HT-041",
        "metric": "outlet_temperature",
        "value": 72.4,
        "unit": "C",
        "observed_at": "2026-12-18T05:00:00+08:00",
        "revision": 3,
    }
    payload.update(overrides)
    return envelope(**payload)


class UnitTests(unittest.TestCase):
    def test_pressure_units_normalize_to_kpa(self):
        self.assertEqual(convert(to_decimal(2.34, "v"), "bar", "pressure")[0], to_decimal(234, "v"))
        self.assertEqual(convert(to_decimal(0.41, "v"), "MPa", "pressure")[0], to_decimal(410, "v"))

    def test_unknown_and_mismatched_units_are_errors(self):
        with self.assertRaises(ValueError):
            convert(to_decimal(1, "v"), "furlong", "pressure")
        with self.assertRaises(ValueError):
            convert(to_decimal(1, "v"), "C", "pressure")


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry(REGISTRY)

    def ingest(self, sources):
        envelopes, issues = load_sources(_write_temp(sources))
        result = Ingestor(self.registry).ingest(envelopes)
        result.source_issues.extend(issues)
        return result

    def test_alias_and_legacy_unit_are_normalized(self):
        result = self.ingest([reading(asset_id="HT041", unit="degC", value=77, revision=4)])
        rec = result.records[0]
        self.assertEqual(rec.asset_id, "HT-041")
        self.assertEqual(str(rec.normalized_value), "77")
        codes = {i.code for i in rec.issues}
        self.assertIn("asset_alias_used", codes)
        self.assertIn("unit_converted", codes)
        self.assertFalse(rec.rejected)

    def test_unknown_asset_is_rejected_not_dropped(self):
        result = self.ingest([reading(asset_id="HT-041A")])
        self.assertEqual(len(result.records), 0)
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.rejected[0].issues[0].code, "unresolved_asset")

    def test_observed_after_collection_is_rejected(self):
        result = self.ingest(
            [reading(observed_at="2026-12-18T05:30:00+08:00")]  # 采集于 05:20
        )
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.rejected[0].issues[0].code, "observed_after_collection")

    def test_weather_with_unknown_area_is_rejected(self):
        result = self.ingest(
            [
                envelope(
                    source_id="w-1",
                    kind="weather_alert",
                    area="NOWHERE",
                    starts_at="2026-12-18T06:00:00+08:00",
                    ends_at="2026-12-18T08:00:00+08:00",
                    alert="ice-risk",
                )
            ]
        )
        self.assertEqual(result.rejected[0].issues[0].code, "unresolved_area")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report = build_report(REGISTRY, SOURCES, as_of="2026-12-18T06:30:00+08:00")
        self.assets = {a["asset_id"]: a for a in self.report["assets"]}

    def test_correction_puts_dependent_dispatch_into_review_without_cancelling(self):
        asset = self.assets["HT-041"]
        dispatch = asset["dispatch"][0]
        self.assertEqual(dispatch["dispatch_id"], "DSP-220")
        self.assertEqual(dispatch["review_status"], "needs_review")
        self.assertEqual(dispatch["effective_state"], "review_required")
        # 安排本身没有被撤销，仍保留原派工班组
        self.assertEqual(dispatch["state"], "assigned")
        self.assertEqual(dispatch["crew"], "C-08")
        self.assertTrue(dispatch["reasons"])

    def test_unrelated_dispatch_stays_intact(self):
        other = self.assets["LN-220"]["dispatch"][0]
        self.assertEqual(other["dispatch_id"], "DSP-221")
        self.assertEqual(other["review_status"], "current")
        self.assertEqual(other["effective_state"], "assigned")
        self.assertFalse(other["basis_complete"] is None)

    def test_earlier_as_of_does_not_see_later_correction(self):
        report = build_report(REGISTRY, SOURCES, as_of="2026-12-18T06:10:00+08:00")
        asset = {a["asset_id"]: a for a in report["assets"]}["HT-041"]
        dispatch = asset["dispatch"][0]
        self.assertEqual(dispatch["review_status"], "current")
        self.assertEqual(asset["monitoring"]["effective"]["revision"], 3)

    def test_maintenance_profile_tightens_thresholds(self):
        eff = self.assets["HT-041"]["monitoring"]["effective"]
        self.assertEqual(eff["profile"], "maintenance")
        self.assertEqual(eff["thresholds"]["alert_high"], 75)
        self.assertEqual(eff["risk"], "alert")

    def test_weather_must_overlap_reading_window(self):
        weather = self.assets["LN-220"]["weather"]
        self.assertEqual(weather["status"], "not_covered")
        codes = {g["code"] for g in self.assets["LN-220"]["gaps"]}
        self.assertIn("alert_not_covering", codes)

    def test_missing_evidence_is_categorised_not_normal(self):
        ht052 = self.assets["HT-052"]
        self.assertEqual(ht052["monitoring"]["status"], "missing")
        self.assertEqual(ht052["assessment"]["level"], "unknown")
        categories = {g["category"] for g in ht052["gaps"]}
        self.assertIn("monitoring", categories)

        gs107 = self.assets["GS-107"]
        categories = {g["category"] for g in gs107["gaps"]}
        self.assertEqual(categories, {"monitoring", "inspection", "weather"})
        self.assertEqual(gs107["inspection"]["status"], "missing")
        self.assertEqual(gs107["weather"]["status"], "no_data")
        self.assertTrue(gs107["assessment"]["indeterminate"])

    def test_dispatch_chain_has_reading_inspection_weather_dispatch(self):
        chain = self.assets["HT-041"]["dispatch"][0]["basis_chain"]
        steps = [c["step"] for c in chain]
        self.assertEqual(steps[:2], ["reading", "inspection"])
        self.assertIn("weather", steps)
        self.assertEqual(steps[-1], "dispatch")

    def test_recheck_queue_points_inspector_at_corrections(self):
        items = self.report["recheck_queue"]
        self.assertTrue(any(i["kind"] == "remeasure" and i["asset_id"] == "HT-041" for i in items))
        self.assertTrue(any(i["kind"] == "reinspect" and i["asset_id"] == "GS-107" for i in items))

    def test_rejected_sources_are_reported(self):
        rejected = {r["source_id"] for r in self.report["rejected_sources"]}
        self.assertIn("meter-feed-legacy", rejected)


class TemporalIsolationTests(unittest.TestCase):
    def test_later_revision_becomes_effective_once_collected(self):
        sources = [
            reading(
                source_id="old",
                collected="2026-12-18T05:50:00+08:00",
                observed_at="2026-12-18T05:40:00+08:00",
                revision=3,
            ),
            reading(
                source_id="new",
                collected="2026-12-18T06:20:00+08:00",
                observed_at="2026-12-18T06:19:00+08:00",
                revision=4,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
            early = build_report(REGISTRY, path, as_of="2026-12-18T06:10:00+08:00")
            late = build_report(REGISTRY, path, as_of="2026-12-18T06:30:00+08:00")
        self.assertEqual(_asset(early, "HT-041")["monitoring"]["effective"]["source_id"], "old")
        self.assertEqual(_asset(late, "HT-041")["monitoring"]["effective"]["source_id"], "new")

    def test_stale_reading_forces_dispatch_review(self):
        sources = [
            reading(source_id="t-old", observed_at="2026-12-18T03:00:00+08:00", revision=1),
            envelope(
                source_id="d-1",
                kind="dispatch",
                collected="2026-12-18T05:00:00+08:00",
                dispatch_id="DSP-9",
                asset_id="HT-041",
                crew="C-01",
                state="assigned",
                basis_source_id="t-old",
                basis_revision=1,
            ),
        ]
        report = build_fixture(sources)
        asset = _asset(report, "HT-041")
        self.assertEqual(asset["monitoring"]["status"], "stale")
        self.assertEqual(asset["dispatch"][0]["review_status"], "needs_review")


class ReportShapeTests(unittest.TestCase):
    def test_report_is_json_serialisable_and_deterministic(self):
        first = build_report(REGISTRY, SOURCES, as_of="2026-12-18T06:30:00+08:00")
        second = build_report(REGISTRY, SOURCES, as_of="2026-12-18T06:30:00+08:00")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_asset_and_area_filters(self):
        report = build_report(REGISTRY, SOURCES, as_of="2026-12-18T06:30:00+08:00", asset_ids=["HT-041"])
        self.assertEqual([a["asset_id"] for a in report["assets"]], ["HT-041"])
        report = build_report(REGISTRY, SOURCES, as_of="2026-12-18T06:30:00+08:00", area_id="EAST-2")
        self.assertEqual([a["asset_id"] for a in report["assets"]], ["LN-220"])

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            build_report(
                REGISTRY,
                SOURCES,
                as_of="2026-12-18T06:30:00+08:00",
                window=("2026-12-18T07:00", "2026-12-18T06:00"),
            )


def _asset(report, asset_id):
    return {a["asset_id"]: a for a in report["assets"]}[asset_id]


def _write_temp(sources):
    import tempfile

    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"sources": sources}, tmp, ensure_ascii=False)
    tmp.close()
    return tmp.name


if __name__ == "__main__":
    unittest.main()
