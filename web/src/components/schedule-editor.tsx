import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Input, Select } from "@/components/ui/form";
import { api, type SchedulePlan } from "@/lib/api";
import { fmtScheduledTime } from "@/lib/format";

export function ScheduleEditor({ plan, onChange, onValidity }: {
  plan: SchedulePlan; onChange: (plan: SchedulePlan) => void; onValidity: (valid: boolean) => void;
}) {
  const [mode, setMode] = useState(plan.cron ? "cron" : plan.cron === null && plan.interval > 0 ? "interval" : "manual");
  const [times, setTimes] = useState<number[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    setTimes(null); setError(""); onValidity(false);
    if ((mode === "interval" && plan.interval < 1) || (mode === "cron" && !plan.cron?.trim())) {
      setError(mode === "interval" ? "间隔至少为 1 分钟" : "请输入 cron，或选择仅手动运行"); return;
    }
    const timer = setTimeout(async () => {
      try {
        const response = await api.post<{ times: number[] }>("/api/schedules/preview", plan);
        if (!cancelled) { setTimes(response.times); onValidity(true); }
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); }
    }, 350);
    return () => { clearTimeout(timer); cancelled = true; };
  }, [plan.cron, plan.interval, plan.timezone, mode, onValidity]);
  const choose = (value: string) => {
    setMode(value);
    onChange({ ...plan, cron: value === "interval" ? null : value === "manual" ? "" : plan.cron || "0 3 * * *",
      interval: value === "interval" ? plan.interval || 60 : 0 });
  };
  return <div className="space-y-3 sm:col-span-2">
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label="执行计划"><Select aria-label="执行计划" value={mode} onChange={e => choose(e.target.value)}>
        <option value="interval">固定间隔</option><option value="cron">Cron 固定时间</option><option value="manual">仅手动运行</option>
      </Select></Field>
      <Field label="时区" hint="例如 Asia/Shanghai"><Input aria-label="时区" value={plan.timezone} onChange={e => onChange({ ...plan, timezone: e.target.value })} /></Field>
      {mode === "interval" && <Field label="间隔（分钟）" hint="从上次发起执行计算间隔"><Input aria-label="间隔（分钟）" type="number" min={1} step={1} value={plan.interval}
        onChange={e => onChange({ ...plan, interval: Number(e.target.value) })} /></Field>}
      {mode === "cron" && <Field label="Cron 表达式" hint="分 时 日 月 周"><Input aria-label="Cron 表达式" value={plan.cron ?? ""} className="font-mono"
        onChange={e => onChange({ ...plan, cron: e.target.value })} /></Field>}
    </div>
    {mode === "cron" && <div className="flex flex-wrap gap-2">
      {[{ label: "每天 03:00", cron: "0 3 * * *" }, { label: "每天 03:00 / 15:00", cron: "0 3,15 * * *" }].map(p =>
        <Button key={p.cron} size="sm" onClick={() => onChange({ ...plan, cron: p.cron })}>{p.label}</Button>)}
    </div>}
    {error ? <p role="alert" className="text-sm text-err">{error}</p> : <div className="text-xs text-muted">
      {times === null ? "正在校验计划…" : times.length ? <><p>接下来计划执行时间（{plan.timezone}）：</p>
        <ul className="mt-1 space-y-1">{times.map(t => <li key={t}>{fmtScheduledTime(t, plan.timezone)}</li>)}</ul></> : "仅手动运行"}
    </div>}
    <p className="text-xs text-muted">预览从当前时间计算，实际下次时间以列表为准。保存只修改计划；已有任务继续执行。实际开始时间可能因排队或执行条件而不同。</p>
  </div>;
}
