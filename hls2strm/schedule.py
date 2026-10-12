"""共用计划模型：五段 cron、固定分钟间隔和仅手动。"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_TIMEZONE = "Asia/Shanghai"


def next_cron_at(cron: str, timezone: str, after: float) -> int:
    return int(croniter(cron, datetime.fromtimestamp(after, ZoneInfo(timezone))).get_next(datetime).timestamp())


class ScheduleFields(BaseModel):
    # None 表示固定间隔，空串表示仅手动，非空字符串表示 cron。
    cron: str | None = None
    timezone: str = DEFAULT_TIMEZONE
    interval: int = Field(60, ge=0)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai") from None
        return value

    @field_validator("cron")
    @classmethod
    def check_cron(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if value and (len(value.split()) != 5 or not croniter.is_valid(value)):
            raise ValueError("cron 必须为五段：分 时 日 月 周，例如 15 3 * * *")
        return value

    @model_validator(mode="after")
    def validate_schedule(self):
        if self.cron is not None:
            self.interval = 0
        if self.cron:
            try:
                next_cron_at(self.cron, self.timezone, datetime.now().timestamp())
            except ValueError:
                raise ValueError("cron 没有有效的下次执行时间，请检查日期组合") from None
        return self


def has_schedule(plan: dict) -> bool:
    return bool(plan["cron"]) if plan.get("cron") is not None else plan["interval"] > 0


def after(plan: dict, anchor: float) -> int:
    if plan.get("cron") is not None:
        return next_cron_at(plan["cron"], plan["timezone"], anchor) if plan["cron"] else 0
    return int(anchor + plan["interval"] * 60) if plan["interval"] > 0 else 0


def preview(plan: ScheduleFields, current: float, count: int = 5) -> list[int]:
    fields = plan.model_dump()
    result = []
    for _ in range(count):
        current = after(fields, current)
        if not current:
            break
        result.append(current)
    return result
