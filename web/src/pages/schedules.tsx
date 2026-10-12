import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ask } from "@/components/confirm";
import { QueryNotice } from "@/components/common";
import { ScheduleEditor } from "@/components/schedule-editor";
import { Button } from "@/components/ui/button";
import { Chip, EmptyRow, Help, Mono, Panel, Table, Td, Th } from "@/components/ui/data";
import { Check, Input, Select } from "@/components/ui/form";
import { Dialog, DialogContent } from "@/components/ui/overlay";
import { api, type ScheduledTask, type SchedulePlan, type ScheduleRun } from "@/lib/api";
import { fmtBytes, fmtScheduledTime, fmtTime } from "@/lib/format";
import { useRun } from "@/lib/queries";
import { navigate } from "@/lib/route";

const STATES: Record<string, string> = { scheduled: "待调度", disabled: "已停用", manual: "仅手动", queued: "已排队", running: "执行中",
  paused: "已暂停", uninitialized: "等待首轮全量", site_off: "站点停用", library_pending: "库变更未完成", no_targets: "无适用对象", active: "已有列表任务", failed: "上次失败" };
const RESULTS: Record<string, string> = { queued: "已排队", running: "执行中", paused: "已暂停", done: "完成", failed: "失败", interrupted: "服务中断", cancelled: "已取消" };
const done = { invalidate: [["schedules"], ["schedule-history"], ["subscriptions"], ["status"], ["jobs"], ["health"]] };

function resultText(task: ScheduledTask, result: ScheduleRun["result"]): string {
  const labels: Record<string, string> = { checked: "检查影片", updated: "更新位置", missing: "未找到", duplicates: "重复影片", extra: "本库以外",
    pulled: "归入", written: "写出", orphans: "残留目录", moved: "处理目录", failed: "处理失败", hosts: "播放站状态数",
    removed: task.key === "system:trash" ? "清理批次" : task.key === "system:play_history" ? "清理记录" : "移除" };
  return Object.entries(result).flatMap(([key, value]) => key === "bytes" && typeof value === "number" ? [`文件量：${fmtBytes(value)}`]
    : labels[key] && typeof value === "number" ? [`${labels[key]}：${value}`] : []).join(" · ");
}

export default function Schedules() {
  const query = useQuery({ queryKey: ["schedules"], queryFn: ({ signal }) => api.get<ScheduledTask[]>("/api/schedules", signal), refetchInterval: 3000 });
  const run = useRun();
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [state, setState] = useState("");
  const [enabled, setEnabled] = useState("");
  const [editing, setEditing] = useState<ScheduledTask | null>(null);
  const [history, setHistory] = useState<ScheduledTask | null>(null);
  const items = (query.data ?? []).filter(p => (!kind || p.kind === kind) && (!state || p.state === state)
    && (!enabled || p.enabled === (enabled === "yes")) && `${p.name} ${p.target}`.toLowerCase().includes(search.trim().toLowerCase()));
  const execute = async (p: ScheduledTask) => {
    if (p.destructive && !(await ask(`立即执行「${p.name}」？`, p.note, { danger: true, confirmText: "确认执行" }))) return;
    await run(() => api.post<{ run_id: number; job_id: number | null }>(`/api/schedules/${p.key}/run`, { confirm: p.destructive }), {
      ...done, success: r => r.job_id ? `已提交任务 #${r.job_id}` : `已提交执行 #${r.run_id}`,
    });
  };
  return <>
    <Panel title="定时任务" bodyClassName="px-4 py-0" actions={<div className="flex flex-wrap gap-2">
      <Input className="w-44" placeholder="搜索任务或输出库" aria-label="搜索定时任务" value={search} onChange={e => setSearch(e.target.value)} />
      <Select value={kind} aria-label="任务类别" onChange={e => setKind(e.target.value)}><option value="">全部类别</option><option value="system">系统任务</option><option value="subscription">订阅</option></Select>
      <Select value={enabled} aria-label="启用状态" onChange={e => setEnabled(e.target.value)}><option value="">全部启用状态</option><option value="yes">已启用</option><option value="no">已停用</option></Select>
      <Select value={state} aria-label="执行状态" onChange={e => setState(e.target.value)}><option value="">全部执行状态</option>{Object.entries(STATES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select>
    </div>}>
      <QueryNotice query={query} label="定时任务" />
      <Table className="min-w-[1050px]"><thead><tr><Th>名称与作用对象</Th><Th>计划</Th><Th>当前状态</Th><Th>上次执行</Th><Th>下次计划</Th><Th>操作</Th></tr></thead>
        <tbody>{items.map(p => <tr key={p.key}>
          <Td><div className="font-medium">{p.name}</div><div className="mt-1 text-xs text-muted">{p.kind === "system" ? "系统任务" : "订阅"} · {p.target}</div>
            {!p.follows_pause && <Chip tone="info" className="mt-1">不随业务暂停</Chip>}
          </Td>
          <Td><Mono>{p.cron || (p.cron === null && p.interval ? `每 ${p.interval} 分钟` : "仅手动")}</Mono>
            <p className="text-xs text-muted">{p.timezone}{p.startup ? " · 启动后执行一次" : ""}</p><p className="mt-1 text-xs text-muted">{p.enabled ? "已启用" : "已停用"}</p>
          </Td>
          <Td><Chip tone={p.state === "failed" ? "err" : p.state === "running" ? "ok" : "neutral"}>{STATES[p.state] ?? p.state}</Chip>
            <p className="mt-1 max-w-52 text-xs text-muted">{p.reason}</p></Td>
          <Td>{p.last_run ? <><p className="text-xs">{fmtTime(p.last_run.started_at ?? p.last_run.dispatched_at)} · {RESULTS[p.last_run.status] ?? p.last_run.status}</p>
            {p.last_run.job_id && <button className="mt-1 text-xs text-accent" onClick={() => navigate("jobs", { job: String(p.last_run!.job_id) })}>任务 #{p.last_run.job_id}</button>}</>
            : p.last_job_id ? <button className="text-xs text-accent" onClick={() => navigate("jobs", { job: String(p.last_job_id) })}>已有任务 #{p.last_job_id}</button> : <span className="text-xs text-muted">尚无执行记录</span>}</Td>
          <Td><span className="text-xs">{p.next_run_at ? fmtScheduledTime(p.next_run_at, p.timezone) : "—"}</span></Td>
          <Td><div className="flex flex-wrap gap-1">
            <Button size="sm" onClick={() => setEditing(p)}>编辑计划</Button>
            <Button size="sm" disabled={run.busy} onClick={() => run(() => api.put(`/api/schedules/${p.key}`, { cron: p.cron, interval: p.interval, timezone: p.timezone, enabled: !p.enabled }), { ...done, success: p.enabled ? "已停用自动计划" : "已启用自动计划" })}>{p.enabled ? "停用" : "启用"}</Button>
            <Button size="sm" title={p.run_blocked_reason} disabled={run.busy || !p.can_run} onClick={() => execute(p)}>立即执行</Button>
            <Button size="sm" onClick={() => setHistory(p)}>历史</Button>
            {p.subscription_id && <Button size="sm" onClick={() => navigate("libraries", { subscription: String(p.subscription_id) })}>订阅配置</Button>}
            {p.library_id && <Button size="sm" onClick={() => navigate("videos", { library: String(p.library_id) })}>输出库</Button>}
          </div></Td>
        </tr>)}{!items.length && !query.isPending && !query.error && <EmptyRow cols={6}>没有符合筛选条件的定时任务</EmptyRow>}</tbody></Table>
      <div className="py-3"><Help summary="调度、暂停与执行记录说明">
        <p>统一执行记录从升级后开始。固定间隔从上次发起执行计算，cron 按所选时区的钟点执行；重启不补跑停机期间的 cron。计划时间可能因为任务排队、首轮初始化或库变更而延后。</p>
        <p>停用计划不取消已有任务。回收区清理和播放记录清理不随业务暂停，可在这里单独停用；归并的事件触发仍保留，停用的是定时兜底。</p>
        <p>系统内部每约 30 秒检查计划并保存连通性状态；启动时统计缺失文件；任务日志执行期间批量写入。这些运行机制无需编辑计划。</p>
      </Help></div>
    </Panel>
    <Dialog open={!!editing} onOpenChange={o => !o && setEditing(null)}>{editing && <EditSchedule key={editing.key} task={editing} close={() => setEditing(null)} />}</Dialog>
    <Dialog open={!!history} onOpenChange={o => !o && setHistory(null)}>{history && <RunHistory key={history.key} task={history} />}</Dialog>
  </>;
}

function EditSchedule({ task, close }: { task: ScheduledTask; close: () => void }) {
  const run = useRun();
  const [plan, setPlan] = useState<SchedulePlan>({ cron: task.cron, interval: task.interval, timezone: task.timezone });
  const [enabled, setEnabled] = useState(task.enabled);
  const [valid, setValid] = useState(false);
  const save = async () => { if (await run(() => api.put(`/api/schedules/${task.key}`, { ...plan, enabled }), { ...done, success: "执行计划已保存" })) close(); };
  return <DialogContent title={`编辑计划 · ${task.name}`} footer={<><Button onClick={close}>取消</Button><Button variant="primary" disabled={!valid || run.busy} onClick={save}>保存计划</Button></>}>
    <fieldset disabled={run.busy} className="space-y-4"><Check checked={enabled} onChange={setEnabled}>启用自动计划</Check>
      <p className="text-sm text-muted">{task.note}</p><ScheduleEditor plan={plan} onChange={setPlan} onValidity={setValid} />
    </fieldset>
  </DialogContent>;
}

function RunHistory({ task }: { task: ScheduledTask }) {
  const [page, setPage] = useState(0);
  const query = useQuery({ queryKey: ["schedule-history", task.key, page], queryFn: ({ signal }) => api.get<{ items: ScheduleRun[]; total: number }>(`/api/schedules/${task.key}/history?limit=20&offset=${page * 20}`, signal), refetchInterval: 3000 });
  return <DialogContent className="max-w-3xl" title={`执行历史 · ${task.name}`}>
    <QueryNotice query={query} label="执行历史" />
    <div className="space-y-3">{query.data?.items.map(r => <div key={r.id} className="rounded border border-line p-3 text-sm">
      <p>{RESULTS[r.status] ?? r.status} · {({ manual: "手动", schedule: "定时", event: "事件" } as Record<string, string>)[r.trigger]} · #{r.id}</p>
      <p className="mt-1 text-xs text-muted">计划 {fmtTime(r.planned_at) || "—"} · 排队 {fmtTime(r.dispatched_at)} · 开始 {fmtTime(r.started_at) || "—"} · 完成 {fmtTime(r.finished_at) || "—"}</p>
      {r.error && <p className="mt-1 text-xs text-err">{r.error}</p>}
      {resultText(task, r.result) && <p className="mt-2 text-xs text-muted">{resultText(task, r.result)}</p>}
      {r.job_id && <Button size="sm" className="mt-2" onClick={() => navigate("jobs", { job: String(r.job_id) })}>查看任务 #{r.job_id}</Button>}
    </div>)}</div>
    {!query.data?.items.length && !query.isPending && !query.error && <p className="text-sm text-muted">尚无统一执行记录，旧任务可从任务页面查看。</p>}
    <div className="mt-4 flex items-center justify-between text-xs text-muted"><Button size="sm" disabled={!page} onClick={() => setPage(page - 1)}>上一页</Button>
      <span>第 {page + 1} 页 · 共 {query.data?.total ?? 0} 次</span><Button size="sm" disabled={!query.data || (page + 1) * 20 >= query.data.total} onClick={() => setPage(page + 1)}>下一页</Button></div>
  </DialogContent>;
}
