"""时间解析与单位换算的共享小工具。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

UTC = timezone.utc


def parse_time(value: str, default_tz: timezone = timezone(timedelta(hours=8))) -> datetime:
    """Accepts ISO-8601 with or without an offset; naive values take default_tz.

    返回值统一落在 ``default_tz``（演练数据本地时区），便于输出与人工核对；
    内部比较仍依赖带时区的 aware datetime，不受显示时区影响。
    """
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间 {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(default_tz)


def minutes_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / 60.0


def to_decimal(value: object, field: str) -> Decimal:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"字段 {field} 的数值 {value!r} 不是数字") from exc
    if not dec.is_finite():
        raise ValueError(f"字段 {field} 的数值 {value!r} 不是有限数字")
    return dec


# 单位 -> (量纲, 换算成基准单位的系数)。基准：温度 °C，压力 kPa。
UNIT_TABLE: dict[str, tuple[str, Decimal]] = {
    "c": ("temperature", Decimal("1")),
    "degc": ("temperature", Decimal("1")),
    "°c": ("temperature", Decimal("1")),
    "celsius": ("temperature", Decimal("1")),
    "f": ("temperature", None),  # 特殊处理
    "degf": ("temperature", None),
    "°f": ("temperature", None),
    "kpa": ("pressure", Decimal("1")),
    "bar": ("pressure", Decimal("100")),
    "mpa": ("pressure", Decimal("1000")),
    "psi": ("pressure", Decimal("6.894757")),
    "kg/cm2": ("pressure", Decimal("98.0665")),
}


class UnitError(ValueError):
    """未登记或与设施量纲不符的单位。"""


def normalize_unit(raw: object) -> str:
    return str(raw).strip().lower().replace(" ", "")


def convert(value: Decimal, raw_unit: object, expected_quantity: str) -> tuple[Decimal, str]:
    """Return (value in canonical unit, canonical unit). Raises UnitError."""
    key = normalize_unit(raw_unit)
    if key not in UNIT_TABLE:
        raise UnitError(f"单位 {raw_unit!r} 未在换算词表中登记")
    quantity, factor = UNIT_TABLE[key]
    if quantity != expected_quantity:
        raise UnitError(f"单位 {raw_unit!r} 属于{quantity}量纲，与设施要求的{expected_quantity}不一致")
    if key in {"f", "degf", "°f"}:
        return (value - Decimal("32")) * Decimal("5") / Decimal("9"), "C"
    canonical = "C" if quantity == "temperature" else "kPa"
    return value * factor, canonical


def fmt_number(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
