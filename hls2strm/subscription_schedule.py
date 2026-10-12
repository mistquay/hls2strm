"""订阅的五段 cron、时区与旧分钟周期兼容。"""

from .schedule import DEFAULT_TIMEZONE, ScheduleFields, after as plan_after, has_schedule, next_cron_at


def scheduled_after(sub: dict, after: float) -> int:
    """cron 按墙上时间触发；旧周期保持按上次运行时间累加。"""
    if sub.get("cron") is not None:
        return plan_after(sub, after)
    if sub["interval"] <= 0:
        return 0
    return plan_after(sub, max(sub["last_run_at"], sub.get("schedule_updated_at", 0))) if sub["last_run_at"] else int(after)


def schedule_anchor(sub: dict, started_at: float) -> float:
    return max(sub["last_run_at"] or 0, sub["created_at"], sub["schedule_updated_at"], started_at)


def next_run_at(sub: dict, current: float, started_at: float = 0) -> int | None:
    if not sub["enabled"] or not sub["initialized"] or not has_schedule(sub):
        return None
    return max(int(current), scheduled_after(sub, schedule_anchor(sub, started_at)))


async def migrate_v14(conn) -> None:
    # 旧订阅保持分钟调度，不强行近似成无法表达相同间隔的 cron。
    await conn.execute("ALTER TABLE subscriptions ADD COLUMN cron TEXT")
    await conn.execute("ALTER TABLE subscriptions ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'")
    await conn.execute("ALTER TABLE subscriptions ADD COLUMN schedule_updated_at INTEGER NOT NULL DEFAULT 0")
