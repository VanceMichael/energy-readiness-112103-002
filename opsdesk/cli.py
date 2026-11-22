"""采集工作台的命令行入口。

用法示例：
    python3 -m opsdesk --as-of 2026-12-18T06:30:00+08:00
    python3 -m opsdesk --asset HT-041 --window 2026-12-18T05:00/2026-12-18T07:00
    python3 -m opsdesk --serve --port 8765
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evidence import build_report
from .units import parse_time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "fixtures" / "registry.json"
DEFAULT_SOURCES = ROOT / "fixtures" / "sources.json"


def _window(text: str) -> tuple[str, str]:
    parts = text.split("/")
    if len(parts) != 2 or not all(parts):
        raise argparse.ArgumentTypeError("时间窗口格式应为 开始/结束，例如 2026-12-18T05:00/2026-12-18T07:00")
    return parts[0], parts[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsdesk", description="迎峰期巡检与监测依据工作台")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="设施台账 JSON")
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES), help="来源资料 JSON")
    parser.add_argument("--as-of", dest="as_of", help="调度判断时点，默认取最新采集时间")
    parser.add_argument("--window", type=_window, help="查看时段，格式 开始/结束")
    parser.add_argument("--asset", action="append", dest="assets", help="只看指定设施，可重复")
    parser.add_argument("--area", help="只看指定片区")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--serve", action="store_true", help="启动本地只读工作台（需 --port）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if args.serve:
        from .server import serve

        return serve(args.host, args.port, args.registry, args.sources)

    try:
        report = build_report(
            args.registry,
            args.sources,
            as_of=args.as_of,
            window=args.window,
            asset_ids=args.assets,
            area_id=args.area,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.format == "json":
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(render_text(report))
    return 0


LEVEL_TAG = {"alert": "【告警】", "watch": "【关注】", "normal": "【正常】", "unknown": "【待补证】"}


def render_text(report: dict) -> str:
    lines: list[str] = []
    summary = report["summary"]
    lines.append(f"调度判断时点：{_local(report['as_of'])}")
    if report["window"]["start"]:
        lines.append(f"查看时段：{_local(report['window']['start'])} → {_local(report['window']['end'])}")
    lines.append(
        "设施 {asset_count} 处｜告警 {alert_assets}｜关注 {watch_assets}｜待补证 {indeterminate_assets}"
        "｜处置 {dispatch_total}（需复核 {dispatch_needs_review}）｜复测项 {recheck_items}".format(**summary)
    )
    lines.append("")

    for asset in report["assets"]:
        assessment = asset["assessment"]
        lines.append(
            f"{LEVEL_TAG.get(assessment['level'], '')} {asset['asset_id']} {asset['name']}"
            f"（{asset['area_name']}，{asset['kind_label']}）"
        )
        lines.append(f"  结论：{assessment['summary']}")
        if asset["maintenance"]:
            m = asset["maintenance"]
            lines.append(f"  检修：{m['event_id']} {m['label']}（{_local(m['started_at'])} → {_local(m['ended_at'])}）")

        mon = asset["monitoring"]
        eff = mon["effective"]
        if eff:
            lines.append(
                f"  监测：{eff['metric_label']} {eff['value']} {eff['unit']} "
                f"（{eff['profile_label']}，{_local(eff['observed_at'])}，时效 {mon['status_label']}）"
                f" → {eff['risk_label']}：{eff['risk_reason']}"
            )
            for hist in mon["history"][-3:]:
                marker = "已被修正" if hist["superseded_by"] else "历史"
                lines.append(
                    f"    · {marker} {hist['source_id']} 修订 {hist['revision']}：{hist['value']} {hist['unit']}"
                    f"（{_local(hist['observed_at'])}）"
                )
        else:
            lines.append(f"  监测：{mon['status_label']}，没有可用读数")

        insp = asset["inspection"]
        if insp["source_id"]:
            lines.append(
                f"  现场：{insp['check_id'] or insp['source_id']} {insp['condition_label']}"
                f"（{_local(insp['observed_at'])}，{insp['status_label']}）"
            )
        else:
            lines.append(f"  现场：{insp['status_label']}")

        wx = asset["weather"]
        alert_text = "；".join(
            f"{a['alert_label']} {_local(a['starts_at'])} → {_local(a['ends_at'])}" for a in wx["alerts"]
        )
        lines.append(f"  气象：{wx['status_label']}" + (f"｜{alert_text}" if alert_text else ""))

        for gap in asset["gaps"]:
            lines.append(f"    ! {gap['category_label']}：{gap['message']}")

        for dispatch in asset["dispatch"]:
            lines.append(
                f"  处置：{dispatch['dispatch_id']} 班组 {dispatch['crew']}｜{dispatch['review_label']}"
                f"｜有效状态 {dispatch['effective_state']}"
            )
            for reason in dispatch["reasons"]:
                lines.append(f"    ↳ {reason}")
        lines.append("")

    if report["rejected_sources"]:
        lines.append("未通过校验的来源（未作为依据）：")
        for item in report["rejected_sources"]:
            reasons = "；".join(i["message"] for i in item["issues"])
            lines.append(f"  - {item['source_id']}（{item['record_kind']}）：{reasons}")
        lines.append("")

    if report["recheck_queue"]:
        lines.append("复测 / 补证队列：")
        for item in report["recheck_queue"]:
            lines.append(
                f"  [{item['priority']}] {item['asset_id']} {item['kind_label']}：{item['reason']}"
            )
    return "\n".join(lines)


def _local(value: str | None) -> str:
    if not value:
        return "—"
    from datetime import timedelta, timezone

    return parse_time(value).astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
