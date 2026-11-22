"""依据链、时效、覆盖与处置复核的核心计算。

关键口径（与 README 一致）：
* 一条监测读数的“有效窗口”取 ``[observed_at, observed_at + 采样周期]``；
  气象预警只有与这个窗口相交，才算覆盖该读数的判断依据。
* 时效判断一律相对 ``as_of``，不相对当前系统时间，便于演练复现。
* 缺资料不会被折算成“正常”：分别记入监测连续性、现场检查、预警覆盖三类缺口。
* 修正读数只会让引用旧版本的处置安排进入复核，不会撤销任何安排；
  与该设备无关的备援保持原状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .ingest import IngestResult, Record
from .registry import Asset, MaintenanceWindow, Registry, max_risk, risk_rank
from .units import fmt_number, minutes_between, parse_time

GAP_LABELS = {
    "monitoring": "监测连续性",
    "inspection": "现场检查",
    "weather": "预警覆盖",
}

LEVEL_LABELS = {
    "normal": "正常",
    "watch": "关注",
    "alert": "告警",
    "unknown": "待补证",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Gap:
    category: str
    code: str
    message: str
    blocking: bool = False

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "category_label": GAP_LABELS.get(self.category, self.category),
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass
class ReadingView:
    record: Record
    metric_label: str
    metric_unit: str
    profile: str
    profile_label: str
    thresholds: dict
    value: Decimal | None
    risk: str
    risk_reason: str
    valid_from: datetime | None
    valid_to: datetime | None
    effective: bool
    superseded_by: str | None = None

    def to_dict(self) -> dict:
        rec = self.record
        return {
            "source_id": rec.source_id,
            "revision": rec.revision,
            "observed_at": rec.observed_at.isoformat() if rec.observed_at else None,
            "collected_at": rec.collected_at.isoformat(),
            "value": fmt_number(self.value) if self.value is not None else None,
            "unit": self.metric_unit,
            "raw_value": rec.raw_value,
            "raw_unit": rec.raw_unit,
            "metric": rec.metric,
            "metric_label": self.metric_label,
            "profile": self.profile,
            "profile_label": self.profile_label,
            "thresholds": self.thresholds,
            "risk": self.risk,
            "risk_label": LEVEL_LABELS.get(self.risk, self.risk),
            "risk_reason": self.risk_reason,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "effective": self.effective,
            "superseded_by": self.superseded_by,
            "issues": [i.to_dict() for i in rec.issues],
        }


@dataclass
class MonitoringView:
    status: str  # fresh | stale | missing
    effective: ReadingView | None
    history: list[ReadingView]
    age_minutes: float | None
    max_age_minutes: int
    cadence_minutes: int | None
    gaps: list[dict] = field(default_factory=list)
    issues: list[Gap] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "status_label": {
                "fresh": "时效内",
                "stale": "已过期",
                "missing": "缺监测",
            }.get(self.status, self.status),
            "age_minutes": round(self.age_minutes, 1) if self.age_minutes is not None else None,
            "max_age_minutes": self.max_age_minutes,
            "cadence_minutes": self.cadence_minutes,
            "effective": self.effective.to_dict() if self.effective else None,
            "history": [r.to_dict() for r in self.history],
            "continuity_gaps": self.gaps,
            "issues": [g.to_dict() for g in self.issues],
        }


@dataclass
class InspectionView:
    status: str  # fresh | stale | missing
    record: Record | None
    age_minutes: float | None
    interval_minutes: int
    condition: str | None
    condition_label: str | None
    severity: str
    issues: list[Gap] = field(default_factory=list)

    def to_dict(self) -> dict:
        rec = self.record
        return {
            "status": self.status,
            "status_label": {"fresh": "时效内", "stale": "已过期", "missing": "缺现场检查"}.get(
                self.status, self.status
            ),
            "age_minutes": round(self.age_minutes, 1) if self.age_minutes is not None else None,
            "interval_minutes": self.interval_minutes,
            "source_id": rec.source_id if rec else None,
            "check_id": rec.check_id if rec else None,
            "observed_at": rec.observed_at.isoformat() if rec and rec.observed_at else None,
            "condition": self.condition,
            "condition_label": self.condition_label,
            "severity": self.severity,
            "issues": [g.to_dict() for g in self.issues],
        }


@dataclass
class WeatherView:
    status: str  # covered | partial | not_covered | no_data
    alerts: list[Record]
    coverage_start: datetime | None
    coverage_end: datetime | None
    risk: str
    issues: list[Gap] = field(default_factory=list)

    def to_dict(self, registry: Registry) -> dict:
        return {
            "status": self.status,
            "status_label": {
                "covered": "已覆盖",
                "partial": "部分覆盖",
                "not_covered": "预警时段未覆盖读数窗口",
                "no_data": "该片区无预警资料",
            }.get(self.status, self.status),
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
            "risk": self.risk,
            "alerts": [
                {
                    "source_id": a.source_id,
                    "alert": a.alert,
                    "alert_label": registry.alert_labels.get(a.alert, a.alert),
                    "area_id": a.area_id,
                    "starts_at": a.starts_at.isoformat() if a.starts_at else None,
                    "ends_at": a.ends_at.isoformat() if a.ends_at else None,
                    "revision": a.revision,
                    "severity": registry.alert_severity.get(a.alert, "unknown"),
                    "collected_at": a.collected_at.isoformat(),
                }
                for a in self.alerts
            ],
            "issues": [g.to_dict() for g in self.issues],
        }


@dataclass
class DispatchView:
    record: Record
    basis_source_id: str | None
    basis_revision: int | None
    basis_record: Record | None
    current_revision: int | None
    status: str  # current | needs_review | basis_unknown | basis_ahead
    effective_state: str
    reasons: list[str] = field(default_factory=list)
    chain: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        rec = self.record
        return {
            "dispatch_id": rec.dispatch_id,
            "source_id": rec.source_id,
            "asset_id": rec.asset_id,
            "crew": rec.crew,
            "state": rec.state,
            "effective_state": self.effective_state,
            "review_status": self.status,
            "review_label": {
                "current": "依据版本有效" if not self.reasons else "依据版本有效，但不完整",
                "needs_review": "依据已过期，需复核（未撤销）",
                "basis_unknown": "依据版本不明，需人工核对",
                "basis_ahead": "依据版本超前于已采集读数",
            }.get(self.status, self.status),
            "basis_complete": not self.reasons,
            "basis_source_id": self.basis_source_id,
            "basis_revision": self.basis_revision,
            "current_revision": self.current_revision,
            "collected_at": rec.collected_at.isoformat(),
            "reasons": self.reasons,
            "basis_chain": self.chain,
        }


@dataclass
class AssetEvidence:
    asset: Asset
    maintenance: MaintenanceWindow | None
    monitoring: MonitoringView
    inspection: InspectionView
    weather: WeatherView
    risk_level: str
    indeterminate: bool
    gaps: list[Gap]
    dispatches: list[DispatchView]

    def to_dict(self, registry: Registry) -> dict:
        return {
            "asset_id": self.asset.asset_id,
            "name": self.asset.name,
            "kind": self.asset.kind,
            "kind_label": registry.kind_labels.get(self.asset.kind, self.asset.kind),
            "area_id": self.asset.area_id,
            "area_name": registry.area(self.asset.area_id).get("name", self.asset.area_id),
            "assessment": {
                "level": self.risk_level,
                "level_label": LEVEL_LABELS.get(self.risk_level, self.risk_level),
                "indeterminate": self.indeterminate,
                "summary": _assessment_summary(self.risk_level, self.indeterminate, self.gaps),
            },
            "maintenance": (
                {
                    "event_id": self.maintenance.event_id,
                    "label": self.maintenance.label,
                    "started_at": self.maintenance.started_at.isoformat(),
                    "ended_at": self.maintenance.ended_at.isoformat(),
                    "status": self.maintenance.status,
                }
                if self.maintenance
                else None
            ),
            "monitoring": self.monitoring.to_dict(),
            "inspection": self.inspection.to_dict(),
            "weather": self.weather.to_dict(registry),
            "gaps": [g.to_dict() for g in self.gaps],
            "dispatch": [d.to_dict() for d in self.dispatches],
        }


def _assessment_summary(level: str, indeterminate: bool, gaps: list[Gap]) -> str:
    if level == "unknown":
        return "无可用监测读数，不能视为正常"
    label = LEVEL_LABELS.get(level, level)
    if indeterminate:
        missing = "、".join(sorted({GAP_LABELS.get(g.category, g.category) for g in gaps}))
        return f"现有依据{label}，但{missing}不完整，结论待补证"
    return f"依据齐备，判为{label}"


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class EvidenceBook:
    def __init__(self, registry: Registry, ingest: IngestResult):
        self.registry = registry
        self.ingest = ingest
        self.records = ingest.records
        self.rejected = ingest.rejected
        self._accepted_ids = {r.source_id for r in self.records}
        self._superseded_by: dict[str, Record] = {}
        for rec in self.records:
            if rec.supersedes and rec.supersedes in self._accepted_ids:
                self._superseded_by[rec.supersedes] = rec

    def _superseded_known_at(self, known: list[Record]) -> dict[str, Record]:
        """只认调度时点前已到货的修正，后到修正不改变历史时点的判断。"""
        known_ids = {r.source_id for r in known}
        return {
            original: corrector
            for original, corrector in self._superseded_by.items()
            if corrector.source_id in known_ids
        }

    # ---- 公共入口 ------------------------------------------------------
    def default_as_of(self) -> datetime:
        if not self.records:
            return datetime.now().astimezone()
        return max(r.collected_at for r in self.records)

    def build(
        self,
        as_of: datetime | None = None,
        window: tuple[datetime, datetime] | None = None,
        asset_ids: list[str] | None = None,
        area_id: str | None = None,
    ) -> dict:
        as_of = as_of or self.default_as_of()
        assets = [
            a
            for a in self.registry.assets.values()
            if (not asset_ids or a.asset_id in asset_ids) and (not area_id or a.area_id == area_id)
        ]
        assets.sort(key=lambda a: (a.area_id, a.asset_id))

        evidence = [self.asset_evidence(asset, as_of, window) for asset in assets]
        dispatches = [d for e in evidence for d in e.dispatches]
        recheck = self.recheck_queue(evidence, as_of)
        rejected_view = [self._rejected_view(r) for r in self.rejected]

        unverified = []
        for item in evidence:
            for gap in item.gaps:
                unverified.append(
                    {
                        "asset_id": item.asset.asset_id,
                        "asset_name": item.asset.name,
                        "category": gap.category,
                        "category_label": GAP_LABELS.get(gap.category, gap.category),
                        "code": gap.code,
                        "message": gap.message,
                        "blocking": gap.blocking,
                    }
                )
            for issue in item.monitoring.issues:
                if issue.code not in {g.code for g in item.gaps}:
                    unverified.append(
                        {
                            "asset_id": item.asset.asset_id,
                            "asset_name": item.asset.name,
                            "category": issue.category,
                            "category_label": GAP_LABELS.get(issue.category, issue.category),
                            "code": issue.code,
                            "message": issue.message,
                            "blocking": issue.blocking,
                        }
                    )

        summary = {
            "asset_count": len(evidence),
            "alert_assets": sum(1 for e in evidence if e.risk_level == "alert"),
            "watch_assets": sum(1 for e in evidence if e.risk_level == "watch"),
            "indeterminate_assets": sum(1 for e in evidence if e.indeterminate),
            "dispatch_total": len(dispatches),
            "dispatch_needs_review": sum(1 for d in dispatches if d.status == "needs_review"),
            "recheck_items": len(recheck),
            "rejected_sources": len(self.rejected),
        }

        return {
            "as_of": as_of.isoformat(),
            "window": {
                "start": window[0].isoformat() if window else None,
                "end": window[1].isoformat() if window else None,
            },
            "summary": summary,
            "assets": [e.to_dict(self.registry) for e in evidence],
            "dispatch": [d.to_dict() for d in dispatches],
            "unverified": unverified,
            "rejected_sources": rejected_view,
            "recheck_queue": recheck,
            "registry_warnings": self.registry.warnings,
        }

    # ---- 单设施 --------------------------------------------------------
    def _records_known_at(self, as_of: datetime) -> list[Record]:
        """只把调度时点之前已采集到的记录作为可见依据。"""
        return [r for r in self.records if r.collected_at <= as_of]

    def asset_evidence(self, asset: Asset, as_of: datetime, window=None) -> AssetEvidence:
        known = self._records_known_at(as_of)
        telemetry = [
            r
            for r in known
            if r.record_kind == "telemetry" and r.asset_id == asset.asset_id
        ]
        inspections = [
            r
            for r in known
            if r.record_kind == "inspection" and r.asset_id == asset.asset_id
        ]
        alerts = [
            r
            for r in known
            if r.record_kind == "weather_alert" and r.area_id == asset.area_id
        ]
        dispatches = [
            r for r in known if r.record_kind == "dispatch" and r.asset_id == asset.asset_id
        ]

        superseded = self._superseded_known_at(known)
        monitoring = self._monitoring(asset, telemetry, as_of, window, superseded)
        inspection = self._inspection(asset, inspections, as_of, window)
        weather = self._weather(asset, alerts, as_of, monitoring)
        maintenance = self.registry.active_maintenance(asset.asset_id, as_of)

        gaps: list[Gap] = []
        gaps.extend(monitoring.issues)
        gaps.extend(inspection.issues)
        gaps.extend(weather.issues)
        gaps.extend(self._maintenance_gaps(asset, maintenance, monitoring, inspection, as_of))

        reading_risk = monitoring.effective.risk if monitoring.effective else "unknown"
        inspection_risk = inspection.severity if inspection.status == "fresh" else "unknown"
        weather_risk = weather.risk if weather.status in {"covered", "partial"} else "unknown"
        combined = max_risk(reading_risk, inspection_risk, weather_risk)
        # 没有可用读数时，仅凭现场/气象不能把设施判为“正常”或“关注”；
        # 除非现场已明确给出告警级发现，否则保留“待补证”。
        if monitoring.effective is None and risk_rank(combined) < risk_rank("alert"):
            combined = "unknown"
        indeterminate = bool(gaps) and risk_rank(combined) <= risk_rank("watch")
        if combined == "unknown":
            indeterminate = True

        dispatch_views = [
            self._dispatch(asset, d, monitoring, inspection, weather, known, superseded)
            for d in dispatches
        ]

        return AssetEvidence(
            asset=asset,
            maintenance=maintenance,
            monitoring=monitoring,
            inspection=inspection,
            weather=weather,
            risk_level=combined,
            indeterminate=indeterminate,
            gaps=gaps,
            dispatches=dispatch_views,
        )

    # ---- 监测 ----------------------------------------------------------
    def _primary_metric(self, asset: Asset):
        for key, metric in asset.metrics.items():
            if not key.endswith("@maintenance"):
                return metric
        return None

    def _metric_for(self, asset: Asset, metric_key: str | None, observed_at: datetime | None):
        if metric_key is None:
            return self._primary_metric(asset)
        key = metric_key
        if observed_at is not None and self.registry.active_maintenance(asset.asset_id, observed_at):
            profile_metric = asset.metrics.get(f"{metric_key}@maintenance")
            if profile_metric is not None:
                return profile_metric
        metric = asset.metric(key)
        if metric is None:
            profile_metric = asset.metrics.get(f"{key}@maintenance")
            return profile_metric
        return metric

    def _classify(self, value: Decimal, metric) -> tuple[str, str]:
        v = float(value)
        if v >= metric.alert_high:
            return "alert", f"{fmt_number(value)} ≥ 告警上限 {metric.alert_high:g}{metric.unit}"
        if v >= metric.watch_high:
            return "watch", f"{fmt_number(value)} ≥ 关注上限 {metric.watch_high:g}{metric.unit}"
        if v > metric.normal_max:
            return "watch", f"{fmt_number(value)} 高于正常上限 {metric.normal_max:g}{metric.unit}"
        if v < metric.normal_min:
            return "watch", f"{fmt_number(value)} 低于正常下限 {metric.normal_min:g}{metric.unit}"
        return "normal", f"{fmt_number(value)} 在 {metric.normal_min:g}–{metric.normal_max:g}{metric.unit} 区间内"

    def _reading_views(
        self,
        asset: Asset,
        telemetry: list[Record],
        as_of: datetime | None = None,
        superseded: dict[str, Record] | None = None,
    ) -> dict[str, list[ReadingView]]:
        if superseded is None:
            superseded = self._superseded_by
        grouped: dict[str, list[Record]] = {}
        for rec in telemetry:
            grouped.setdefault(rec.metric or "?", []).append(rec)

        views: dict[str, list[ReadingView]] = {}
        for metric_key, recs in grouped.items():
            recs.sort(key=lambda r: (r.revision, r.collected_at, r.source_id))
            usable = [
                r for r in recs if as_of is None or r.observed_at is None or r.observed_at <= as_of
            ]
            effective_rec = (
                max(usable, key=lambda r: (r.revision, r.collected_at, r.source_id))
                if usable
                else None
            )
            for rec in recs:
                metric = self._metric_for(asset, metric_key, rec.observed_at)
                profile = metric.profile if metric else "normal"
                profile_label = metric.profile_label if metric else "常规口径"
                thresholds = (
                    {
                        "normal_min": metric.normal_min,
                        "normal_max": metric.normal_max,
                        "watch_high": metric.watch_high,
                        "alert_high": metric.alert_high,
                        "unit": metric.unit,
                    }
                    if metric
                    else {}
                )
                if rec.normalized_value is not None and metric is not None:
                    risk, reason = self._classify(rec.normalized_value, metric)
                elif rec.normalized_value is not None:
                    risk, reason = "unknown", "缺少台账阈值，无法判断风险"
                else:
                    risk, reason = "unknown", "读数不可用"
                cadence = metric.cadence_minutes if metric else None
                valid_from = rec.observed_at
                valid_to = (
                    rec.observed_at + timedelta(minutes=cadence)
                    if rec.observed_at and cadence
                    else None
                )
                superseded_by = superseded.get(rec.source_id)
                views.setdefault(metric_key, []).append(
                    ReadingView(
                        record=rec,
                        metric_label=metric.label if metric else metric_key,
                        metric_unit=metric.unit if metric else (rec.canonical_unit or ""),
                        profile=profile,
                        profile_label=profile_label,
                        thresholds=thresholds,
                        value=rec.normalized_value,
                        risk=risk,
                        risk_reason=reason,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        effective=rec is effective_rec and superseded_by is None,
                        superseded_by=superseded_by.source_id if superseded_by else None,
                    )
                )
        return views

    def _monitoring(
        self,
        asset: Asset,
        telemetry: list[Record],
        as_of: datetime,
        window,
        superseded: dict[str, Record] | None = None,
    ) -> MonitoringView:
        metric = self._primary_metric(asset)
        views_by_metric = self._reading_views(asset, telemetry, as_of, superseded)
        if metric:
            views = views_by_metric.get(metric.key, [])
        else:
            views = [v for vs in views_by_metric.values() for v in vs]
        effective = next((v for v in views if v.effective), None)
        history = [v for v in views if not v.effective]

        max_age = asset.telemetry_max_age_minutes
        cadence = metric.cadence_minutes if metric else self.registry.defaults.get(
            "telemetry_cadence_minutes", 30
        )
        issues: list[Gap] = []
        gaps: list[dict] = []

        if effective is None or effective.value is None or effective.valid_from is None:
            status = "missing"
            age = None
            issues.append(
                Gap(
                    "monitoring",
                    "no_usable_reading",
                    f"{asset.name} 在 {as_of.isoformat()} 前没有可用监测读数，不能视为正常",
                    blocking=True,
                )
            )
        else:
            age = minutes_between(effective.valid_from, as_of)
            if age > max_age:
                status = "stale"
                issues.append(
                    Gap(
                        "monitoring",
                        "reading_stale",
                        f"最新读数 {effective.record.source_id} 已过期 {age:.0f} 分钟（阈值 {max_age} 分钟），风险可能已变化",
                        blocking=True,
                    )
                )
            else:
                status = "fresh"

            # 连续性：窗口内相邻读数间隔超过 1.5 倍采样周期即记缺口。
            if cadence:
                considered = sorted(
                    (
                        v
                        for v in views
                        if v.record.observed_at
                        and v.record.observed_at <= as_of
                        and (window is None or window[0] <= v.record.observed_at <= window[1])
                    ),
                    key=lambda v: v.record.observed_at,
                )
                start = window[0] if window else (
                    considered[0].record.observed_at if considered else as_of
                )
                end = min(window[1], as_of) if window else as_of
                cursor = start
                for view in considered:
                    obs = view.record.observed_at
                    if cursor and minutes_between(cursor, obs) > cadence * 1.5:
                        gap_minutes = minutes_between(cursor, obs)
                        gaps.append(
                            {
                                "from": cursor.isoformat(),
                                "to": obs.isoformat(),
                                "minutes": round(gap_minutes, 1),
                                "reason": f"相邻读数间隔 {gap_minutes:.0f} 分钟，超过采样周期 {cadence} 分钟的 1.5 倍",
                            }
                        )
                    cursor = obs
                if cursor and end and minutes_between(cursor, end) > cadence * 1.5:
                    gap_minutes = minutes_between(cursor, end)
                    gaps.append(
                        {
                            "from": cursor.isoformat(),
                            "to": end.isoformat(),
                            "minutes": round(gap_minutes, 1),
                            "reason": (
                                f"截至调度时点已有 {gap_minutes:.0f} 分钟无新读数"
                                if not window or window[1] >= as_of
                                else f"窗口结束前已有 {gap_minutes:.0f} 分钟无新读数"
                            ),
                        }
                    )
                elif cursor is None and end:
                    gaps.append(
                        {
                            "from": start.isoformat() if start else None,
                            "to": end.isoformat(),
                            "minutes": None,
                            "reason": "窗口内没有任何监测读数",
                        }
                    )
                for gap in gaps:
                    issues.append(
                        Gap(
                            "monitoring",
                            "continuity_gap",
                            f"监测连续性缺口 {gap['from']} → {gap['to']}：{gap['reason']}",
                        )
                    )

        # 修正链问题
        rejected_fixes = [
            r
            for r in self.rejected
            if r.supersedes
            and r.collected_at <= as_of
            and self._is_successor_of_asset(r, asset)
        ]
        for rec in rejected_fixes:
            issues.append(
                Gap(
                    "monitoring",
                    "rejected_correction",
                    f"后到修正 {rec.source_id} 未通过校验，旧读数仍在使用，需人工核实",
                    blocking=True,
                )
            )
        # 同设备同版本冲突
        by_rev: dict[int, set[str]] = {}
        for view in views:
            if view.value is None:
                continue
            by_rev.setdefault(view.record.revision, set()).add(fmt_number(view.value))
        for revision, values in by_rev.items():
            if len(values) > 1:
                issues.append(
                    Gap(
                        "monitoring",
                        "revision_conflict",
                        f"修订 {revision} 存在 {len(values)} 个不同读数（{'/'.join(sorted(values))}），无法确认有效值",
                        blocking=True,
                    )
                )

        return MonitoringView(
            status=status,
            effective=effective,
            history=history,
            age_minutes=age,
            max_age_minutes=max_age,
            cadence_minutes=cadence,
            gaps=gaps,
            issues=issues,
        )

    def _is_successor_of_asset(self, record: Record, asset: Asset) -> bool:
        """被拒收的修正通常设备编号也不可解析，退回按原始 payload 判断归属。"""
        if record.asset_id == asset.asset_id:
            return True
        raw_asset = record.payload.get("asset_id")
        resolved = self.registry.resolve_asset(raw_asset)
        return resolved is not None and resolved.asset_id == asset.asset_id

    # ---- 现场检查 ------------------------------------------------------
    def _inspection(
        self, asset: Asset, inspections: list[Record], as_of: datetime, window
    ) -> InspectionView:
        considered = [
            r
            for r in inspections
            if r.observed_at and r.observed_at <= as_of and (window is None or window[0] <= r.observed_at <= window[1])
        ]
        considered.sort(key=lambda r: (r.observed_at, r.collected_at))
        latest = considered[-1] if considered else None
        interval = asset.inspection_interval_minutes
        issues: list[Gap] = []
        if latest is None:
            return InspectionView(
                status="missing",
                record=None,
                age_minutes=None,
                interval_minutes=interval,
                condition=None,
                condition_label=None,
                severity="unknown",
                issues=[
                    Gap(
                        "inspection",
                        "inspection_missing",
                        f"{asset.name} 没有可用现场检查记录，缺的是现场检查而非“正常”",
                        blocking=True,
                    )
                ],
            )

        age = minutes_between(latest.observed_at, as_of)
        condition = latest.condition
        severity = self.registry.condition_severity.get(condition, "unknown")
        label = self.registry.condition_labels.get(condition, condition)
        if age > interval:
            status = "stale"
            issues.append(
                Gap(
                    "inspection",
                    "inspection_stale",
                    f"最近现场检查 {latest.check_id or latest.source_id} 已过 {age:.0f} 分钟（巡检周期 {interval} 分钟），需复检",
                )
            )
        else:
            status = "fresh"
        if severity in {"watch", "alert"}:
            issues.append(
                Gap(
                    "inspection",
                    "inspection_finding",
                    f"现场检查发现“{label}”，风险等级 {severity}",
                    blocking=severity == "alert",
                )
            )
        return InspectionView(
            status=status,
            record=latest,
            age_minutes=age,
            interval_minutes=interval,
            condition=condition,
            condition_label=label,
            severity=severity,
            issues=issues,
        )

    # ---- 气象预警 ------------------------------------------------------
    def _weather(
        self, asset: Asset, alerts: list[Record], as_of: datetime, monitoring: MonitoringView
    ) -> WeatherView:
        applicable = [
            a
            for a in alerts
            if a.alert and a.starts_at and a.ends_at and a.collected_at <= as_of
        ]
        effective = monitoring.effective
        if effective and effective.valid_from:
            cov_start = effective.valid_from
            cov_end = effective.valid_to or effective.valid_from
        else:
            # 没有读数时，气象依据只能对着“当前时点”核查，并明确标成缺监测下的近似核对。
            cov_start = cov_end = as_of

        if not applicable:
            area_name = self.registry.area(asset.area_id).get("name", asset.area_id)
            return WeatherView(
                status="no_data",
                alerts=[],
                coverage_start=cov_start,
                coverage_end=cov_end,
                risk="unknown",
                issues=[
                    Gap(
                        "weather",
                        "alert_missing",
                        f"{area_name} 没有可用的气象预警资料，预警覆盖未知，不能视为无风险",
                    )
                ],
            )

        overlapping = []
        for alert in applicable:
            if alert.starts_at <= cov_end and alert.ends_at >= cov_start:
                overlapping.append(alert)
        # 只在同一片区内的预警已由调用方筛选
        if not overlapping:
            others = [a for a in applicable if a.starts_at > cov_end]
            if others:
                nearest = min(others, key=lambda a: a.starts_at)
                message = (
                    f"最近预警 {nearest.alert} 自 {nearest.starts_at.isoformat()} 起生效，"
                    f"不覆盖读数有效窗口 {cov_start.isoformat()}–{cov_end.isoformat()}；"
                    "该窗口内的气象依据缺失"
                )
            else:
                message = "已有预警均在读数有效窗口之前结束，该窗口内的气象依据缺失"
            return WeatherView(
                status="not_covered",
                alerts=applicable,
                coverage_start=cov_start,
                coverage_end=cov_end,
                risk="unknown",
                issues=[Gap("weather", "alert_not_covering", message)],
            )

        risk = max_risk(*(self.registry.alert_severity.get(a.alert, "unknown") for a in overlapping))
        issues: list[Gap] = []
        fully = any(a.starts_at <= cov_start and a.ends_at >= cov_end for a in overlapping)
        status = "covered" if fully else "partial"
        if not fully:
            issues.append(
                Gap(
                    "weather",
                    "alert_partial_cover",
                    f"预警仅部分覆盖读数有效窗口 {cov_start.isoformat()}–{cov_end.isoformat()}，窗口边缘依据不完整",
                )
            )
        return WeatherView(
            status=status,
            alerts=overlapping,
            coverage_start=cov_start,
            coverage_end=cov_end,
            risk=risk,
            issues=issues,
        )

    # ---- 检修口径 ------------------------------------------------------
    def _maintenance_gaps(
        self,
        asset: Asset,
        maintenance: MaintenanceWindow | None,
        monitoring: MonitoringView,
        inspection: InspectionView,
        as_of: datetime,
    ) -> list[Gap]:
        gaps: list[Gap] = []
        if not maintenance:
            return gaps
        effective = monitoring.effective
        if effective and effective.record.observed_at:
            if effective.record.observed_at < maintenance.started_at:
                gaps.append(
                    Gap(
                        "monitoring",
                        "profile_changed_after_reading",
                        f"读数形成于检修 {maintenance.event_id} 开始之前，检修后同一数值对应风险已变化，需按新口径复测",
                        blocking=True,
                    )
                )
            elif effective.profile != "maintenance":
                gaps.append(
                    Gap(
                        "monitoring",
                        "profile_not_applied",
                        f"读数处于检修 {maintenance.event_id} 时段，但未套用检修口径阈值",
                    )
                )
        if inspection.record and inspection.record.observed_at:
            if inspection.record.observed_at < maintenance.started_at:
                gaps.append(
                    Gap(
                        "inspection",
                        "inspection_before_maintenance",
                        f"最近现场检查早于检修 {maintenance.event_id} 开始，检修后的设备状态尚未现场确认",
                    )
                )
        return gaps

    # ---- 处置 ----------------------------------------------------------
    def _dispatch(
        self,
        asset: Asset,
        dispatch: Record,
        monitoring: MonitoringView,
        inspection: InspectionView,
        weather: WeatherView,
        known: list[Record],
        superseded: dict[str, Record],
    ) -> DispatchView:
        reasons: list[str] = []
        effective = monitoring.effective
        current_revision = effective.record.revision if effective else None
        basis_record: Record | None = None
        basis_rid = dispatch.basis_source_id
        basis_revision = dispatch.basis_revision

        telemetry = [
            r
            for r in known
            if r.record_kind == "telemetry" and r.asset_id == asset.asset_id
        ]
        if basis_rid:
            basis_record = next((r for r in telemetry if r.source_id == basis_rid), None)
            if basis_record is not None:
                basis_revision = basis_record.revision
            else:
                rejected = next((r for r in self.rejected if r.source_id == basis_rid), None)
                if rejected:
                    reasons.append(f"依据来源 {basis_rid} 未通过校验，无法确认依据")
        elif basis_revision is not None:
            candidates = [r for r in telemetry if r.revision == basis_revision]
            if not candidates:
                reasons.append(f"找不到修订 {basis_revision} 的监测读数，依据来源不明")
            else:
                basis_record = max(candidates, key=lambda r: r.collected_at)

        if basis_record is None and basis_revision is None:
            status = "basis_unknown"
        elif basis_record is None:
            status = "basis_unknown"
        elif current_revision is None:
            status = "basis_unknown"
            reasons.append("该设备当前没有可用监测读数，无法确认依据是否仍有效")
        elif basis_record.source_id in superseded:
            corrector = superseded[basis_record.source_id]
            status = "needs_review"
            reasons.append(
                f"依据读数 {basis_record.source_id}（修订 {basis_record.revision}）已被后到修正 "
                f"{corrector.source_id}（修订 {corrector.revision}）取代"
            )
        elif basis_record.revision < current_revision:
            status = "needs_review"
            reasons.append(
                f"依据修订 {basis_record.revision} 早于当前有效修订 {current_revision}"
            )
        elif basis_record.revision > current_revision:
            status = "basis_ahead"
            reasons.append(
                f"依据修订 {basis_record.revision} 超前于已采集的最新修订 {current_revision}"
            )
        else:
            status = "current"

        if status != "needs_review" and monitoring.status == "stale":
            status = "needs_review"
            reasons.append("监测读数已过期，处置依据不能确认仍然有效")
        if status != "needs_review" and weather.status == "not_covered":
            reasons.append("气象预警未覆盖读数窗口，气象依据不完整")
        if status != "needs_review" and inspection.status == "missing":
            reasons.append("缺少现场检查记录，处置依据不完整")

        effective_state = "review_required" if status == "needs_review" else (dispatch.state or "unknown")

        chain = self._dispatch_chain(
            asset, dispatch, basis_record, monitoring, inspection, weather, status
        )
        return DispatchView(
            record=dispatch,
            basis_source_id=basis_rid or (basis_record.source_id if basis_record else None),
            basis_revision=basis_revision,
            basis_record=basis_record,
            current_revision=current_revision,
            status=status,
            effective_state=effective_state,
            reasons=reasons,
            chain=chain,
        )

    def _dispatch_chain(
        self,
        asset: Asset,
        dispatch: Record,
        basis_record: Record | None,
        monitoring: MonitoringView,
        inspection: InspectionView,
        weather: WeatherView,
        status: str,
    ) -> list[dict]:
        chain: list[dict] = []
        effective = monitoring.effective
        if effective:
            chain.append(
                {
                    "step": "reading",
                    "at": effective.record.observed_at.isoformat() if effective.record.observed_at else None,
                    "source_id": effective.record.source_id,
                    "detail": (
                        f"监测读数 {fmt_number(effective.value)} {effective.metric_unit}"
                        f"（修订 {effective.record.revision}，{effective.profile_label}）；"
                        f"{effective.risk_reason} → {LEVEL_LABELS.get(effective.risk, effective.risk)}"
                    ),
                    "issues": [i.to_dict() for i in effective.record.issues],
                }
            )
        if inspection.record:
            chain.append(
                {
                    "step": "inspection",
                    "at": inspection.record.observed_at.isoformat() if inspection.record.observed_at else None,
                    "source_id": inspection.record.source_id,
                    "detail": (
                        f"现场检查 {inspection.record.check_id or inspection.record.source_id}："
                        f"{inspection.condition_label}（班组 {inspection.record.crew or '未注明'}）"
                    ),
                    "issues": [i.to_dict() for i in inspection.record.issues],
                }
            )
        for alert in weather.alerts:
            chain.append(
                {
                    "step": "weather",
                    "at": alert.starts_at.isoformat() if alert.starts_at else None,
                    "source_id": alert.source_id,
                    "detail": (
                        f"气象预警 {self.registry.alert_labels.get(alert.alert, alert.alert)}"
                        f"（{alert.area_id}，{alert.starts_at.isoformat() if alert.starts_at else '?'}"
                        f"–{alert.ends_at.isoformat() if alert.ends_at else '?'}）"
                    ),
                    "issues": [i.to_dict() for i in alert.issues],
                }
            )
        if basis_record is not None:
            chain.append(
                {
                    "step": "basis",
                    "at": basis_record.collected_at.isoformat(),
                    "source_id": basis_record.source_id,
                    "detail": (
                        f"处置安排引用监测修订 {basis_record.revision}"
                        f"（当前有效修订 {monitoring.effective.record.revision if monitoring.effective else '无'}）"
                    ),
                    "issues": [],
                }
            )
        chain.append(
            {
                "step": "dispatch",
                "at": dispatch.collected_at.isoformat(),
                "source_id": dispatch.source_id,
                "detail": (
                    f"处置安排 {dispatch.dispatch_id} 派班组 {dispatch.crew or '未注明'}，"
                    f"状态 {dispatch.state or '未注明'}，复核结论 "
                    f"{'需复核（未撤销）' if status == 'needs_review' else '依据当前有效'}"
                ),
                "issues": [i.to_dict() for i in dispatch.issues],
            }
        )
        return chain

    # ---- 复测队列与拒收来源 -------------------------------------------
    def recheck_queue(self, evidence: list[AssetEvidence], as_of: datetime) -> list[dict]:
        items: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def add(asset: Asset, kind: str, source_id: str | None, reason: str, priority: str = "normal"):
            key = (asset.asset_id, kind + ":" + (source_id or ""))
            if key in seen:
                return
            seen.add(key)
            items.append(
                {
                    "asset_id": asset.asset_id,
                    "asset_name": asset.name,
                    "area_id": asset.area_id,
                    "kind": kind,
                    "kind_label": {
                        "remeasure": "现场复测",
                        "reinspect": "补做现场检查",
                        "continuity": "补齐监测连续性",
                        "weather_coverage": "确认预警覆盖",
                        "verify_dispatch": "核对处置依据",
                        "verify_record": "核实记录质量",
                    }.get(kind, kind),
                    "source_id": source_id,
                    "reason": reason,
                    "priority": priority,
                }
            )

        for item in evidence:
            asset = item.asset
            for view in [item.monitoring.effective, *item.monitoring.history]:
                if view is None:
                    continue
                rec = view.record
                for issue in rec.issues:
                    if issue.severity == "warning":
                        add(asset, "verify_record", rec.source_id, f"{issue.message}", "low")
                if view.superseded_by:
                    add(
                        asset,
                        "remeasure",
                        view.superseded_by,
                        f"读数 {rec.source_id}（修订 {rec.revision}）已被修正，需现场复测确认修正值",
                        "high",
                    )
                if view.effective and view.risk == "alert":
                    add(asset, "remeasure", rec.source_id, f"最新读数判为告警：{view.risk_reason}", "high")
                elif view.effective and view.risk == "watch" and item.maintenance:
                    add(
                        asset,
                        "remeasure",
                        rec.source_id,
                        f"检修口径下读数判为关注：{view.risk_reason}，建议复测确认",
                        "normal",
                    )
            if item.monitoring.status in {"missing", "stale"}:
                add(asset, "continuity", None, "监测读数缺失或过期，需要补采", "high")
            for gap in item.monitoring.gaps:
                add(
                    asset,
                    "continuity",
                    None,
                    f"监测连续性缺口：{gap['from']} → {gap['to']}",
                    "normal",
                )
            if item.inspection.status in {"missing", "stale"}:
                add(
                    asset,
                    "reinspect",
                    item.inspection.record.source_id if item.inspection.record else None,
                    "现场检查缺失或超过巡检周期，需补检",
                    "high",
                )
            if item.weather.status in {"no_data", "not_covered"}:
                add(
                    asset,
                    "weather_coverage",
                    None,
                    "气象预警未覆盖读数有效窗口，需向气象席确认",
                    "normal",
                )
            for dispatch in item.dispatches:
                if dispatch.status == "needs_review":
                    add(
                        asset,
                        "verify_dispatch",
                        dispatch.record.source_id,
                        f"处置安排 {dispatch.record.dispatch_id} 依据已过期：" + "；".join(dispatch.reasons),
                        "high",
                    )
        order = {"high": 0, "normal": 1, "low": 2}
        items.sort(key=lambda i: (order.get(i["priority"], 9), i["area_id"], i["asset_id"], i["kind"]))
        return items

    def _rejected_view(self, record: Record) -> dict:
        return {
            "source_id": record.source_id,
            "record_kind": record.record_kind,
            "collected_at": record.collected_at.isoformat(),
            "supersedes": record.supersedes,
            "asset_id": record.asset_id,
            "issues": [i.to_dict() for i in record.issues],
        }


def build_report(
    registry_path,
    sources_path,
    as_of: str | None = None,
    window: tuple[str, str] | None = None,
    asset_ids: list[str] | None = None,
    area_id: str | None = None,
):
    from .ingest import Ingestor, load_sources
    from .registry import load_registry

    registry = load_registry(registry_path)
    envelopes, envelope_issues = load_sources(sources_path)
    result = Ingestor(registry).ingest(envelopes)
    result.source_issues.extend(envelope_issues)
    book = EvidenceBook(registry, result)

    def to_dt(value):
        return parse_time(value, registry.default_tz) if value else None

    as_of_dt = to_dt(as_of)
    win = None
    if window and (window[0] or window[1]):
        start = to_dt(window[0]) if window[0] else None
        end = to_dt(window[1]) if window[1] else None
        if start is None or end is None:
            raise ValueError("时间窗口需要同时给出开始和结束时间")
        if start >= end:
            raise ValueError("时间窗口的开始必须早于结束")
        win = (start, end)
    report = book.build(as_of=as_of_dt, window=win, asset_ids=asset_ids, area_id=area_id)
    report["source_issues"] = [i.to_dict() for i in result.source_issues]
    return report
