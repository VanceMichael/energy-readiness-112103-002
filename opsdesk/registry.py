"""设施/片区台账与别名解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .units import parse_time

RISK_ORDER = {"unknown": 0, "normal": 1, "watch": 2, "alert": 3, "critical": 4}


def risk_rank(level: str) -> int:
    return RISK_ORDER.get(level, 0)


def max_risk(*levels: str) -> str:
    best = "unknown"
    for level in levels:
        if risk_rank(level) > risk_rank(best):
            best = level
    return best


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    quantity: str
    unit: str
    normal_min: float
    normal_max: float
    watch_high: float
    alert_high: float
    cadence_minutes: int | None = None
    profile: str = "normal"
    profile_label: str = "常规口径"


@dataclass(frozen=True)
class Asset:
    asset_id: str
    name: str
    kind: str
    area_id: str
    aliases: tuple[str, ...]
    inspection_interval_minutes: int
    telemetry_max_age_minutes: int
    metrics: dict[str, Metric]

    def metric(self, key: str) -> Metric | None:
        return self.metrics.get(key)


@dataclass(frozen=True)
class MaintenanceWindow:
    event_id: str
    asset_id: str
    started_at: object
    ended_at: object
    status: str
    profile: str
    label: str

    def covers(self, when) -> bool:
        return self.started_at <= when <= self.ended_at

    @property
    def open_ended(self) -> bool:
        return self.status == "in_progress"


@dataclass
class Registry:
    areas: dict[str, dict]
    area_aliases: dict[str, str] = field(default_factory=dict)
    assets: dict[str, Asset] = field(default_factory=dict)
    asset_aliases: dict[str, str] = field(default_factory=dict)
    maintenance: list[MaintenanceWindow] = field(default_factory=list)
    condition_severity: dict[str, str] = field(default_factory=dict)
    condition_labels: dict[str, str] = field(default_factory=dict)
    alert_severity: dict[str, str] = field(default_factory=dict)
    alert_labels: dict[str, str] = field(default_factory=dict)
    kind_labels: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, int] = field(default_factory=dict)
    default_tz: object = None
    warnings: list[str] = field(default_factory=list)

    # ---- alias resolution -------------------------------------------------
    @staticmethod
    def _norm_alias(value: object) -> str:
        return str(value).strip().upper().replace(" ", "").replace("_", "-")

    def resolve_asset(self, raw: object) -> Asset | None:
        key = self._norm_alias(raw)
        asset_id = self.asset_aliases.get(key)
        if asset_id is None:
            asset_id = self.asset_aliases.get(key.replace("-", ""))
        return self.assets.get(asset_id) if asset_id else None

    def resolve_area(self, raw: object) -> str | None:
        key = self._norm_alias(raw)
        return self.area_aliases.get(key) or self.area_aliases.get(key.replace("-", ""))

    def area(self, area_id: str) -> dict:
        return self.areas.get(area_id, {"area_id": area_id, "name": area_id})

    # ---- maintenance ------------------------------------------------------
    def maintenance_windows(self, asset_id: str) -> list[MaintenanceWindow]:
        return [w for w in self.maintenance if w.asset_id == asset_id]

    def active_maintenance(self, asset_id: str, observed_at) -> MaintenanceWindow | None:
        """在 observed_at 有效的检修窗口（含开放结束的进行中窗口）。"""
        matches = []
        for window in self.maintenance_windows(asset_id):
            if window.started_at <= observed_at and (
                window.covers(observed_at) or window.open_ended
            ):
                matches.append(window)
        if not matches:
            return None
        return max(matches, key=lambda w: w.started_at)

    def maintenance_at(self, asset_id: str, when) -> MaintenanceWindow | None:
        for window in self.maintenance_windows(asset_id):
            if window.covers(when):
                return window
        return None


def _register_alias(mapping: dict[str, str], alias: object, target: str, kind: str, warnings: list[str]) -> None:
    key = Registry._norm_alias(alias)
    if not key:
        return
    keys = {key}
    compact = key.replace("-", "")
    if compact:
        keys.add(compact)
    for candidate in keys:
        existing = mapping.get(candidate)
        if existing is not None and existing != target:
            warnings.append(
                f"{kind}别名 {alias!r} 同时指向 {existing} 与 {target}，已保留先登记者"
            )
            continue
        mapping[candidate] = target


def load_registry(path: str | Path) -> Registry:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    warnings: list[str] = []
    from datetime import timedelta, timezone

    raw_tz = doc.get("default_timezone", "+08:00")
    sign = 1 if not raw_tz.startswith("-") else -1
    hh, mm = raw_tz.lstrip("+-").split(":")
    default_tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))

    registry = Registry(
        areas={},
        condition_severity=dict(doc.get("condition_severity", {})),
        condition_labels=dict(doc.get("condition_labels", {})),
        alert_severity=dict(doc.get("alert_severity", {})),
        alert_labels=dict(doc.get("alert_labels", {})),
        kind_labels=dict(doc.get("asset_kind_labels", {})),
        defaults=dict(doc.get("defaults", {})),
        default_tz=default_tz,
        warnings=warnings,
    )

    for area in doc.get("areas", []):
        area_id = area["area_id"]
        registry.areas[area_id] = area
        _register_alias(registry.area_aliases, area_id, area_id, "片区", warnings)
        for alias in area.get("aliases", []):
            _register_alias(registry.area_aliases, alias, area_id, "片区", warnings)

    for raw_asset in doc.get("assets", []):
        metrics: dict[str, Metric] = {}
        for metric_key, m in raw_asset.get("metrics", {}).items():
            maint = m.get("maintenance")
            if maint:
                metrics[f"{metric_key}@maintenance"] = Metric(
                    key=metric_key,
                    label=m["label"],
                    quantity=m["quantity"],
                    unit=m["unit"],
                    normal_min=maint["normal_min"],
                    normal_max=maint["normal_max"],
                    watch_high=maint["watch_high"],
                    alert_high=maint["alert_high"],
                    cadence_minutes=m.get("telemetry_cadence_minutes"),
                    profile="maintenance",
                    profile_label=maint.get("label", "检修口径"),
                )
            metrics[metric_key] = Metric(
                key=metric_key,
                label=m["label"],
                quantity=m["quantity"],
                unit=m["unit"],
                normal_min=m["normal_min"],
                normal_max=m["normal_max"],
                watch_high=m["watch_high"],
                alert_high=m["alert_high"],
                cadence_minutes=m.get("telemetry_cadence_minutes"),
            )
        asset = Asset(
            asset_id=raw_asset["asset_id"],
            name=raw_asset["name"],
            kind=raw_asset["kind"],
            area_id=raw_asset["area_id"],
            aliases=tuple(raw_asset.get("aliases", [])),
            inspection_interval_minutes=raw_asset.get(
                "inspection_interval_minutes", registry.defaults.get("inspection_max_age_minutes", 480)
            ),
            telemetry_max_age_minutes=raw_asset.get(
                "telemetry_max_age_minutes", registry.defaults.get("telemetry_max_age_minutes", 90)
            ),
            metrics=metrics,
        )
        registry.assets[asset.asset_id] = asset
        _register_alias(registry.asset_aliases, asset.asset_id, asset.asset_id, "设施", warnings)
        for alias in asset.aliases:
            _register_alias(registry.asset_aliases, alias, asset.asset_id, "设施", warnings)

    for raw_window in doc.get("maintenance", []):
        if raw_window.get("asset_id") not in registry.assets:
            warnings.append(f"检修窗口 {raw_window.get('event_id')} 指向未登记设施 {raw_window.get('asset_id')}")
            continue
        registry.maintenance.append(
            MaintenanceWindow(
                event_id=raw_window["event_id"],
                asset_id=raw_window["asset_id"],
                started_at=parse_time(raw_window["started_at"], default_tz),
                ended_at=parse_time(raw_window["ended_at"], default_tz),
                status=raw_window.get("status", "planned"),
                profile=raw_window.get("profile", "maintenance"),
                label=raw_window.get("label", ""),
            )
        )

    return registry
