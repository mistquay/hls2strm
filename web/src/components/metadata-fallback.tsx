import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { useLibraries, useRun } from "@/lib/queries";
import { navigate, replaceParams, useRoute } from "@/lib/route";
import { fmtTime } from "@/lib/format";
import { ask } from "./confirm";
import { Button } from "./ui/button";
import { Check, Field, Input, Select } from "./ui/form";
import { Panel } from "./ui/data";

const modes = {
  missing: ["补缺失 NFO / 图片", "只补外部目录中缺失的文件，不改已有 NFO。"],
  fields: ["已有 NFO 补空字段", "仅补有效 NFO 的空字段，已有值和扩展信息保留；不下载图片、不移动文件。"],
  takeover: ["接管未整理文件", "从收件目录及可选失败目录接管到外部目录 /_hls补全/，核验完成后移除原文件。"],
} as const;
type Mode = keyof typeof modes;
interface RepairJob {
  id: number; kind: string; name: string; status: string; created_at: number;
  state: { ready?: boolean; preview_token?: string; eligible?: number; skipped?: number;
    checked?: number; done?: number; failed?: number; error?: string; partial_failure?: boolean; apply_job_id?: number };
}
interface Item {
  id: number; state: string; error: string; code: string; title?: string; source: string; target?: string;
  changes: { field: string; before: unknown; after: unknown }[];
  files: { path: string; action: string }[]; warnings: string[];
}
const states: Record<string, string> = { planned: "待确认", skipped: "跳过", running: "进行中", paused: "已暂停", done: "完成", failed: "失败", cancelled: "已取消" };
const fields: Record<string, string> = { title: "标题", originaltitle: "原始标题", num: "番号", maker: "厂商", director: "导演", duration_minutes: "时长（分钟）", categories: "类型", tags: "标签", models: "演员", series: "系列" };
const value = (v: unknown) => Array.isArray(v) ? v.join("、") || "空" : String(v ?? "") || "空";

export function MetadataFallback() {
  const route = useRoute();
  const run = useRun();
  const { data: libraries = [] } = useLibraries(true);
  const [library, setLibrary] = useState(route.params.get("library") || "");
  const [mode, setMode] = useState<Mode>("missing");
  const [images, setImages] = useState(true);
  const [failedDir, setFailedDir] = useState("");
  const [allowEmpty, setAllowEmpty] = useState(false);
  const [paused, setPaused] = useState(false);
  const [page, setPage] = useState(0);
  const [historyPage, setHistoryPage] = useState(0);
  const jobId = Number(route.params.get("job")) || undefined;
  const openJob = (id?: number) => {
    const params = new URLSearchParams(route.params);
    if (id) params.set("job", String(id)); else params.delete("job");
    replaceParams(params); setPage(0); setPaused(false);
  };
  const history = useQuery({
    queryKey: ["metadata-jobs", historyPage],
    queryFn: () => api.get<{ items: RepairJob[]; total: number }>(`/api/metadata/jobs?offset=${historyPage * 20}`),
    refetchInterval: 3000,
  });
  const detail = useQuery({
    queryKey: ["metadata-job", jobId, page],
    queryFn: () => api.get<{ job: RepairJob; request: { library_id: number; mode: Mode; failed_dir: string }; items: Item[]; total: number }>(`/api/metadata/jobs/${jobId}?offset=${page * 50}`),
    enabled: !!jobId, refetchInterval: 3000,
  });
  const refresh = { invalidate: [["metadata-jobs"], ["metadata-job"], ["jobs"], ["libraries"], ["maintenance-assets"]] };
  const job = detail.data?.job;
  const active = !!job && ["running", "paused"].includes(job.status);
  const create = async () => {
    const result = await run(() => api.post<{ job_id: number }>("/api/metadata/preview", {
      library_id: Number(library), mode, images: mode !== "fields" && images, failed_dir: mode === "takeover" ? failedDir.trim() : "",
      allow_empty_external: mode === "takeover" && allowEmpty,
    }), refresh);
    if (result) openJob(result.job_id);
  };
  const execute = async () => {
    if (!job || !(await ask("确认执行源站资料补全", `执行预览 #${job.id} 的全部 ${job.state.eligible || 0} 个可处理条目（不限于当前页）。已变动或冲突的文件跳过；请保持对应外部刮削暂停。`))) return;
    const result = await run(() => api.post<{ job_id: number }>(`/api/metadata/jobs/${job.id}/apply`, {
      preview_token: job.state.preview_token, scraper_paused: paused,
    }), refresh);
    if (result) openJob(result.job_id);
  };
  const act = (action: string) => job && run(() => api.post(`/api/jobs/${job.id}/${action}`), refresh);
  const chosen = libraries.find(lib => String(lib.id) === library);
  return <div className="space-y-4">
    <Button onClick={() => navigate("maintenance", library ? { library } : {})}>返回库维护</Button>
    <Panel title="源站资料兜底补全 · 仅手动">
      <p className="py-3 text-sm text-muted">三个模式独立检查、独立确认。使用数据库已有资料，不重抓详情，不修改外部刮削器配置，不设置定时任务。</p>
      <div className="grid gap-3 md:grid-cols-3">
        {(Object.keys(modes) as Mode[]).map(key => <button key={key} type="button" aria-pressed={mode === key}
          className={`rounded-lg border p-4 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${mode === key ? "border-accent bg-accent/10" : "border-line"}`}
          onClick={() => { setMode(key); openJob(); }}>
          <b className="text-sm">{modes[key][0]}</b><p className="mt-2 text-xs text-muted">{modes[key][1]}</p>
        </button>)}
      </div>
      <div className="grid gap-4 py-4 md:grid-cols-2">
        <Field label="外部整理库"><Select value={library} onChange={e => { setLibrary(e.target.value); openJob(); }}>
          <option value="">请选择一个库</option>
          {libraries.filter(lib => lib.external_dir).map(lib => <option key={lib.id} value={lib.id}>{lib.name}</option>)}
        </Select></Field>
        {mode === "takeover" && <Field label="额外失败目录（可选）" hint="服务端可见的绝对路径；明确归属当前库。留空只检查收件目录。">
          <Input value={failedDir} onChange={e => { setFailedDir(e.target.value); openJob(); }} />
        </Field>}
      </div>
      {chosen && <p className="break-all pb-3 text-xs text-muted">收件：{chosen.root}<br />整理：{chosen.external_root}</p>}
      {mode !== "fields" && <Check checked={images} onChange={v => { setImages(v); openJob(); }}>确认执行时下载缺失图片（已有图片不覆盖、不自动裁剪）</Check>}
      {mode === "takeover" && <div className="pt-3"><Check checked={allowEmpty} onChange={v => { setAllowEmpty(v); openJob(); }}>已核对挂载，允许接管到没有可识别 STRM 的空外部目录</Check></div>}
      <p className="py-3 text-xs text-muted">没有资料的字段仍会留空；历史发行日期暂不导出，不用标题充当剧情。收件区有文件不等于刮削失败，请自行确认后再接管。链接、共享目录和归属冲突不会自动处理。</p>
      <Button variant="primary" disabled={!chosen?.external_dir || run.busy} onClick={create}>只读检查并生成本模式预览</Button>
    </Panel>
    {(history.error || detail.error) && <p role="alert" className="text-err">{(history.error || detail.error)?.message}</p>}
    {!!jobId && detail.isPending && <p role="status">读取预览…</p>}
    {job && <Panel title={`#${job.id} ${job.name}`}>
      <p className="py-3 text-sm" role="status">{states[job.status] || job.status} · {job.state.ready ? `可执行 ${job.state.eligible || 0}，跳过 ${job.state.skipped || 0}` : `已检查 ${job.state.checked || 0}，完成 ${job.state.done || 0}，失败 ${job.state.failed || 0}`}</p>
      <p className="pb-3 text-xs text-muted">当前显示的是此任务保存的固定范围：库 #{detail.data?.request.library_id} · {modes[detail.data!.request.mode][0]}。表单修改不改变历史任务。</p>
      {job.state.error && <p role="alert" className="text-err">{job.state.error}</p>}
      <div className="flex flex-wrap items-center gap-3 pb-4">
        {active && <><Button disabled={run.busy} onClick={() => act(job.status === "paused" ? "resume" : "pause")}>{job.status === "paused" ? "恢复任务" : "暂停任务"}</Button><Button disabled={run.busy} onClick={() => act("cancel")}>取消后续处理</Button></>}
        {!active && !job.state.apply_job_id && <Check checked={paused} onChange={setPaused}>已暂停对应外部刮削写入</Check>}
        {job.kind === "metadata_preview" && job.status === "done" && job.state.ready && !job.state.apply_job_id && <Button variant="primary" disabled={!paused || !job.state.eligible || run.busy} onClick={execute}>确认执行此预览</Button>}
        {job.state.apply_job_id && <Button onClick={() => openJob(job.state.apply_job_id)}>查看执行 #{job.state.apply_job_id}</Button>}
        {job.kind === "metadata_apply" && !active && <Button disabled={!paused || run.busy} onClick={() => run(() => api.post(`/api/metadata/jobs/${job.id}/retry`, { preview_token: "", scraper_paused: paused }), refresh)}>重试未完成项</Button>}
        <Button onClick={() => navigate("jobs", { job: String(job.id) })}>任务日志</Button>
      </div>
      <div className="space-y-3">{detail.data?.items.map(item => <details key={item.id} className="rounded-lg border border-line p-3">
        <summary className="cursor-pointer break-words text-sm"><b>{item.code || "未知影片"}</b> · {states[item.state] || item.state} · {item.title || item.source}{item.error && <span className="ml-2 text-warn">{item.error}</span>}</summary>
        <div className="mt-3 space-y-2 break-all text-xs">
          <p>当前：{item.source}</p>{item.target && <p>目标：{item.target}</p>}
          {item.changes.map(change => <p key={change.field}>{fields[change.field] || change.field}：{value(change.before)} → {value(change.after)}（hls2strm 数据库）</p>)}
          {item.files.map((file, i) => <p key={i}>{file.action}：{file.path}</p>)}
          {item.warnings.map(warning => <p className="text-muted" key={warning}>{warning}</p>)}
        </div>
      </details>)}</div>
      <div className="flex items-center gap-3 py-4"><Button disabled={!page} onClick={() => setPage(page - 1)}>上一页</Button><span className="text-xs">第 {page + 1} 页 · 共 {detail.data?.total || 0} 条</span><Button disabled={(page + 1) * 50 >= (detail.data?.total || 0)} onClick={() => setPage(page + 1)}>下一页</Button></div>
      <p className="pb-3 text-xs text-muted">修改后旧维护索引失效，请重新“扫描磁盘与 NFO”。媒体服务器的展示更新取决于它自身的扫描配置。取消不会撤销已完成文件；接管中断请重试同一执行任务。</p>
    </Panel>}
    <Panel title="补全历史（关闭页面后可继续）">
      <div className="divide-y divide-line">{history.data?.items.map(item => <div key={item.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
        <span className="text-sm">#{item.id} {item.name} · {states[item.status] || item.status}{item.state.partial_failure ? " · 有失败项" : ""}<small className="ml-2 text-muted">{fmtTime(item.created_at)}</small></span>
        <Button size="sm" onClick={() => openJob(item.id)}>查看{item.kind === "metadata_preview" ? "预览" : "结果"}</Button>
      </div>)}</div>
      {!history.data?.items.length && <p className="py-4 text-muted">暂无补全记录</p>}
      <div className="flex items-center gap-3 py-3"><Button disabled={!historyPage} onClick={() => setHistoryPage(historyPage - 1)}>上一页</Button><span className="text-xs">共 {history.data?.total || 0} 条</span><Button disabled={(historyPage + 1) * 20 >= (history.data?.total || 0)} onClick={() => setHistoryPage(historyPage + 1)}>下一页</Button></div>
    </Panel>
  </div>;
}
