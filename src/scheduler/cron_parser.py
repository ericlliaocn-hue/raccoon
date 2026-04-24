"""5位 cron 表达式解析器

支持标准 5 位格式：分 时 日 月 周
- * : 任意值
- */n : 每隔 n
- n-m : 范围
- n,m,... : 列表
- @hourly / @daily / @weekly / @monthly / @yearly : 特殊别名

示例：
  "0 8 * * *"     → 每天 8:00
  "*/30 * * * *"  → 每 30 分钟
  "0 9 * * 1-5"   → 周一到周五 9:00
  "@hourly"       → 每小时整点
"""

from __future__ import annotations

from datetime import datetime

import structlog

logger = structlog.get_logger(__name__)

# 特殊别名
ALIASES: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# 每个字段的取值范围
FIELD_RANGES = [
    (0, 59),   # 分
    (0, 23),   # 时
    (1, 31),   # 日
    (1, 12),   # 月
    (0, 6),    # 周 (0=周日, 6=周六)
]


class CronParseError(ValueError):
    """cron 表达式解析错误"""


class CronParser:
    """5位 cron 表达式解析器"""

    def __init__(self, expression: str) -> None:
        self._raw = expression.strip()

        # 处理别名
        expr = ALIASES.get(self._raw.lower(), self._raw)
        parts = expr.split()

        if len(parts) != 5:
            raise CronParseError(
                f"cron 表达式需要 5 个字段，得到 {len(parts)}: '{self._raw}'"
            )

        self._fields: list[set[int]] = []
        for i, part in enumerate(parts):
            lo, hi = FIELD_RANGES[i]
            self._fields.append(self._parse_field(part, lo, hi))

    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> set[int]:
        """解析单个字段，返回匹配值的集合"""
        values: set[int] = set()

        for item in field.split(","):
            if "/" in item:
                # 步长：*/n 或 n-m/n
                base, step_str = item.split("/", 1)
                try:
                    step = int(step_str)
                except ValueError:
                    raise CronParseError(f"无效步长: {item}")
                if step <= 0:
                    raise CronParseError(f"步长必须为正整数: {item}")

                if base == "*":
                    start, end = lo, hi
                elif "-" in base:
                    start_str, end_str = base.split("-", 1)
                    start, end = int(start_str), int(end_str)
                else:
                    start, end = int(base), hi

                for v in range(start, end + 1, step):
                    if lo <= v <= hi:
                        values.add(v)

            elif "-" in item:
                # 范围：n-m
                start_str, end_str = item.split("-", 1)
                start, end = int(start_str), int(end_str)
                for v in range(start, end + 1):
                    if lo <= v <= hi:
                        values.add(v)

            elif item == "*":
                values.update(range(lo, hi + 1))

            else:
                # 单个数字
                v = int(item)
                if not (lo <= v <= hi):
                    raise CronParseError(f"值 {v} 超出范围 [{lo}, {hi}]")
                values.add(v)

        return values

    def matches(self, dt: datetime) -> bool:
        """检查给定时间是否匹配此 cron 表达式"""
        return (
            dt.minute in self._fields[0]
            and dt.hour in self._fields[1]
            and dt.day in self._fields[2]
            and dt.month in self._fields[3]
            and (dt.weekday() + 1) % 7 in self._fields[4]  # Python weekday: 0=周一 → cron: 0=周日
        )

    def next_time(self, after: datetime) -> datetime:
        """计算从 after 开始的下次触发时间（简单逐分钟扫描）"""
        # 从下一分钟开始
        candidate = after.replace(second=0, microsecond=0)
        candidate = candidate.replace(minute=candidate.minute + 1) if candidate.minute < 59 else candidate.replace(hour=candidate.hour + 1, minute=0) if candidate.hour < 23 else candidate.replace(day=candidate.day + 1, hour=0, minute=0)

        # 最多扫描 366 天（防止无限循环）
        from datetime import timedelta
        limit = after + timedelta(days=366)

        while candidate <= limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)

        # 理论上不会到这里
        return after + timedelta(days=366)

    @property
    def fields(self) -> list[set[int]]:
        return self._fields

    def __repr__(self) -> str:
        return f"CronParser('{self._raw}')"
