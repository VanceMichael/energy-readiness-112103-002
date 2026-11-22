"""原始来源的解析、单位归一与校验。

设计原则：无法可靠解释的记录不会被静默丢弃或当成正常值，而是保留在
``rejected`` 中并附带原因，供值班员在“未核实项”中查看。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .registry import Registry
from .units import UnitError, convert, minutes_between, parse_time, to_decimal

SEVERITY_ORDER = {"info": 0, "warning": 1, "blocking": 2}


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    field_name: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "field": self.field_name,
        }


@dataclass
class SourceEnvelope:
    source_id: str
    record_kind: str
    collected_at: datetime
    payload: dict
    supersedes: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Record:
    source_id: str
    record_kind: str
    collected_at: datetime
    observed_at: datetime | None = None
    asset_id: str | None = None
    area_id: str | None = None
    metric: str | None = None
    revision: int = 1
    supersedes: str | None = None
    normalized_value: Decimal | None = None
    canonical_unit: str | None = None
    raw_value: object = None
    raw_unit: object = None
    condition: str | None = None
    alert: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    crew: str | None = None
    check_id: str | None = None
    dispatch_id: str | None = None
    state: str | None = None
    basis_revision: int | None = None
    basis_source_id: str | None = None
    issues: list[Issue] = field(default_factory=list)
    rejected: bool = False
    payload: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return any(i.severity == "blocking" for i in self.issues)

    def add_issue(self, code: str, severity: str, message: str, field_name: str | None = None) -> None:
        self.issues.append(Issue(code, severity, message, field_name))
        if severity == "blocking":
            self.rejected = True

    def to_dict(self) -> dict:
        def iso(value):
            return value.isoformat() if value else None

        return {
            "source_id": self.source_id,
            "record_kind": self.record_kind,
            "collected_at": iso(self.collected_at),
            "observed_at": iso(self.observed_at),
            "asset_id": self.asset_id,
            "area_id": self.area_id,
            "metric": self.metric,
            "revision": self.revision,
            "supersedes": self.supersedes,
            "value": str(self.normalized_value) if self.normalized_value is not None else None,
            "unit": self.canonical_unit,
            "raw_value": self.raw_value,
            "raw_unit": self.raw_unit,
            "condition": self.condition,
            "alert": self.alert,
            "starts_at": iso(self.starts_at),
            "ends_at": iso(self.ends_at),
            "crew": self.crew,
            "check_id": self.check_id,
            "dispatch_id": self.dispatch_id,
            "state": self.state,
            "basis_revision": self.basis_revision,
            "rejected": self.rejected,
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class IngestResult:
    records: list[Record]
    rejected: list[Record]
    source_issues: list[Issue]
    registry_warnings: list[str]

    def by_kind(self, kind: str) -> list[Record]:
        return [r for r in self.records if r.record_kind == kind]


def load_sources(path: str | Path) -> tuple[list[SourceEnvelope], list[Issue]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    envelopes: list[SourceEnvelope] = []
    issues: list[Issue] = []
    default_tz = None  # 来源时间戳自带偏移；无偏移按注册表默认时区处理
    for index, raw in enumerate(doc.get("sources", [])):
        try:
            source_id = str(raw["source_id"]).strip()
            kind = str(raw["record_kind"]).strip()
            collected = parse_time(raw["collected_at"], default_tz)
            payload = raw.get("payload") or {}
            if not isinstance(payload, dict):
                raise ValueError("payload 不是对象")
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(Issue("invalid_envelope", "warning", f"第 {index + 1} 条来源信封无效：{exc}"))
            continue
        envelopes.append(
            SourceEnvelope(
                source_id=source_id,
                record_kind=kind,
                collected_at=collected,
                payload=payload,
                supersedes=(str(raw["supersedes"]).strip() if raw.get("supersedes") else None),
                raw=raw,
            )
        )
    return envelopes, issues


class Ingestor:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.defaults = registry.defaults

    def ingest(self, envelopes: list[SourceEnvelope]) -> IngestResult:
        records: list[Record] = []
        seen: dict[str, str] = {}
        for envelope in envelopes:
            if envelope.source_id in seen:
                # 同 source_id 重复出现：保留先到记录，后到记入未核实项。
                dup = Record(
                    source_id=envelope.source_id,
                    record_kind=envelope.record_kind,
                    collected_at=envelope.collected_at,
                    payload=envelope.payload,
                )
                dup.add_issue("duplicate_source_id", "warning", f"来源编号 {envelope.source_id} 重复，已保留先到记录")
                records.append(dup)
                continue
            seen[envelope.source_id] = envelope.record_kind
            handler = {
                "telemetry": self._telemetry,
                "inspection": self._inspection,
                "weather_alert": self._weather,
                "dispatch": self._dispatch,
            }.get(envelope.record_kind)
            if handler is None:
                rec = Record(
                    source_id=envelope.source_id,
                    record_kind=envelope.record_kind,
                    collected_at=envelope.collected_at,
                    payload=envelope.payload,
                )
                rec.add_issue(
                    "unknown_record_kind",
                    "blocking",
                    f"记录类型 {envelope.record_kind!r} 未在采集词表中登记",
                )
                records.append(rec)
                continue
            records.append(handler(envelope))
        accepted = [r for r in records if not r.rejected]
        rejected = [r for r in records if r.rejected]
        return IngestResult(
            records=accepted,
            rejected=rejected,
            source_issues=[],
            registry_warnings=list(self.registry.warnings),
        )

    # ------------------------------------------------------------------
    def _base(self, envelope: SourceEnvelope, record_kind: str | None = None) -> Record:
        return Record(
            source_id=envelope.source_id,
            record_kind=record_kind or envelope.record_kind,
            collected_at=envelope.collected_at,
            supersedes=envelope.supersedes,
            payload=envelope.payload,
        )

    def _observed(self, record: Record, payload: dict) -> None:
        raw = payload.get("observed_at")
        if raw is None:
            record.add_issue("missing_observed_at", "blocking", "缺少观测时间，无法确认读数窗口")
            record.observed_at = record.collected_at
            return
        try:
            observed = parse_time(raw, self.registry.default_tz)
        except ValueError as exc:
            record.add_issue("invalid_observed_at", "blocking", str(exc))
            return
        record.observed_at = observed
        if observed > record.collected_at:
            record.add_issue(
                "observed_after_collection",
                "blocking",
                "观测时间晚于采集时间，时序矛盾，无法作为依据",
            )

    def _telemetry(self, envelope: SourceEnvelope) -> Record:
        record = self._base(envelope)
        payload = envelope.payload
        self._observed(record, payload)

        raw_asset = payload.get("asset_id")
        asset = self.registry.resolve_asset(raw_asset)
        if asset is None:
            record.add_issue(
                "unresolved_asset",
                "blocking",
                f"设备编号 {raw_asset!r} 无法对应到台账设施（可能沿用旧名）",
            )
        else:
            record.asset_id = asset.asset_id
            if str(raw_asset).strip().upper() != asset.asset_id:
                record.add_issue(
                    "asset_alias_used",
                    "info",
                    f"来源使用别名 {raw_asset!r}，已归一到 {asset.asset_id}",
                )

        metric_key = str(payload.get("metric", "")).strip()
        record.metric = metric_key or None
        metric = asset.metric(metric_key) if asset else None
        if asset and metric is None:
            record.add_issue(
                "unknown_metric",
                "blocking",
                f"指标 {metric_key!r} 未在设施 {asset.asset_id} 台账中登记",
            )

        record.raw_value = payload.get("value")
        record.raw_unit = payload.get("unit")
        try:
            revision = int(payload.get("revision", 1))
        except (TypeError, ValueError):
            revision = 1
            record.add_issue("invalid_revision", "warning", "修订号无法解析，按 1 处理")
        record.revision = revision

        if metric is not None and record.raw_value is not None:
            try:
                value = to_decimal(record.raw_value, "value")
                normalized, canonical = convert(value, record.raw_unit, metric.quantity)
                record.normalized_value = normalized
                record.canonical_unit = canonical
                if str(record.raw_unit).strip().upper() != canonical.upper():
                    record.add_issue(
                        "unit_converted",
                        "info",
                        f"来源单位 {record.raw_unit!r} 已换算为 {canonical}（原值 {record.raw_value}）",
                    )
            except (UnitError, ValueError) as exc:
                record.add_issue("unit_conflict", "blocking", str(exc), "unit")
        elif record.raw_value is None:
            record.add_issue("missing_value", "blocking", "读数缺少数值", "value")

        self._lag_check(record, "telemetry_report_lag_minutes", "telemetry")
        return record

    def _inspection(self, envelope: SourceEnvelope) -> Record:
        record = self._base(envelope)
        payload = envelope.payload
        self._observed(record, payload)

        raw_asset = payload.get("asset_id")
        asset = self.registry.resolve_asset(raw_asset)
        if asset is None:
            record.add_issue(
                "unresolved_asset",
                "blocking",
                f"设备编号 {raw_asset!r} 无法对应到台账设施（可能沿用旧名）",
            )
        else:
            record.asset_id = asset.asset_id
            if str(raw_asset).strip().upper() != asset.asset_id:
                record.add_issue(
                    "asset_alias_used",
                    "info",
                    f"来源使用别名 {raw_asset!r}，已归一到 {asset.asset_id}",
                )

        record.condition = str(payload.get("condition", "")).strip() or None
        record.crew = str(payload.get("crew", "")).strip() or None
        record.check_id = str(payload.get("check_id", "")).strip() or None

        if record.condition is None:
            record.add_issue("missing_condition", "blocking", "现场检查缺少检查结论", "condition")
        elif record.condition not in self.registry.condition_severity:
            record.add_issue(
                "unknown_condition",
                "blocking",
                f"检查结论 {record.condition!r} 未在词表中登记，无法判断风险等级",
                "condition",
            )
        if not record.crew:
            record.add_issue("missing_crew", "warning", "检查记录缺少班组，追溯依据链会中断", "crew")

        self._lag_check(record, "inspection_report_lag_minutes", "inspection")
        return record

    def _weather(self, envelope: SourceEnvelope) -> Record:
        record = self._base(envelope)
        payload = envelope.payload

        raw_area = payload.get("area")
        area_id = self.registry.resolve_area(raw_area)
        if area_id is None:
            record.add_issue(
                "unresolved_area",
                "blocking",
                f"预警区域 {raw_area!r} 无法对应到片区台账",
            )
        else:
            record.area_id = area_id
            if str(raw_area).strip().upper().replace("-", "") != area_id.replace("-", ""):
                record.add_issue(
                    "area_alias_used",
                    "info",
                    f"来源使用片区别名 {raw_area!r}，已归一到 {area_id}",
                )

        try:
            record.starts_at = parse_time(payload["starts_at"], self.registry.default_tz)
            record.ends_at = parse_time(payload["ends_at"], self.registry.default_tz)
        except (KeyError, ValueError) as exc:
            record.add_issue("invalid_alert_window", "blocking", f"预警生效时段无法解析：{exc}")
        if record.starts_at and record.ends_at and record.starts_at >= record.ends_at:
            record.add_issue(
                "inverted_alert_window",
                "blocking",
                "预警结束时间不晚于开始时间，生效时段无效",
            )

        record.alert = str(payload.get("alert", "")).strip() or None
        if record.alert is None:
            record.add_issue("missing_alert_kind", "blocking", "预警缺少类型", "alert")
        elif record.alert not in self.registry.alert_severity:
            record.add_issue(
                "unknown_alert_kind",
                "blocking",
                f"预警类型 {record.alert!r} 未在词表中登记",
                "alert",
            )

        try:
            record.revision = int(payload.get("revision", 1))
        except (TypeError, ValueError):
            record.revision = 1
            record.add_issue("invalid_revision", "warning", "修订号无法解析，按 1 处理")

        # 天气产品没有“观测时间”，用采集时间做时效判断。
        record.observed_at = record.collected_at
        self._lag_check(record, "weather_report_lag_minutes", "weather")
        return record

    def _dispatch(self, envelope: SourceEnvelope) -> Record:
        record = self._base(envelope)
        payload = envelope.payload
        record.observed_at = record.collected_at

        raw_asset = payload.get("asset_id")
        asset = self.registry.resolve_asset(raw_asset)
        if asset is None:
            record.add_issue(
                "unresolved_asset",
                "blocking",
                f"处置安排指向的设备编号 {raw_asset!r} 无法对应到台账设施",
            )
        else:
            record.asset_id = asset.asset_id

        record.dispatch_id = str(payload.get("dispatch_id", "")).strip() or None
        record.crew = str(payload.get("crew", "")).strip() or None
        record.state = str(payload.get("state", "")).strip() or None
        record.basis_source_id = (
            str(payload["basis_source_id"]).strip() if payload.get("basis_source_id") else None
        )
        if not record.dispatch_id:
            record.add_issue("missing_dispatch_id", "blocking", "处置安排缺少编号")
        if payload.get("basis_revision") is None and not record.basis_source_id:
            record.add_issue(
                "missing_basis",
                "warning",
                "处置安排未注明依据版本，无法自动核对是否基于过期读数",
            )
        elif payload.get("basis_revision") is not None:
            try:
                record.basis_revision = int(payload["basis_revision"])
            except (TypeError, ValueError):
                record.add_issue("invalid_basis_revision", "warning", "依据版本号无法解析")
        return record

    def _lag_check(self, record: Record, default_key: str, kind: str) -> None:
        threshold = int(self.registry.defaults.get(default_key, 10**9))
        observed = record.observed_at
        if observed is None:
            return
        lag = minutes_between(observed, record.collected_at)
        if lag > threshold:
            record.add_issue(
                "report_lag",
                "warning",
                f"{kind} 记录在观测后 {lag:.0f} 分钟才采集（阈值 {threshold} 分钟），时效需复核",
            )
