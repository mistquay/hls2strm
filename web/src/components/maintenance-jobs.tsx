import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtNum, fmtTime } from "@/lib/format";
import { maintenanceStatus, type MaintenanceJob } from "@/lib/maintenance";
import { useLibraries, useRun } from "@/lib/queries";
import { navigate } from "@/lib/route";
import { Button } from "./ui/button";
import { Panel, Table, Td, Th } from "./ui/data";
import { Select } from "./ui/form";

export function MaintenanceJobs({ category = "all", onJob }: {
  category?: "all" | "unavailable";
  onJob: (id: number) => void;
}) {
  const run = useRun();
  const { data: libraries = [] } = useLibraries(true);
  const [page, setPage] = useState(0);
  const [status, setStatus] = useState("");
  const result = useQuery({
    queryKey: ["maintenance-jobs", category, status, page],
    queryFn: ({ signal }) => api.get<{ items: MaintenanceJob[]; total: number }>(
      `/api/maintenance/jobs?category=${category}&status=${status}&offset=${page * 20}`, signal),
    refetchInterval: 3000,
  });
  const restore = async (job: MaintenanceJob) => {
    const response = await run(() => api.post<{ job_id: number }>(`/api/maintenance/jobs/${job.id}/restore-preview`), {
      invalidate: [["maintenance-jobs"], ["maintenance-overview"]],
    });
    if (response) onJob(response.job_id);
  };
  return <Panel id={category === "unavailable" ? "unavailable-tasks" : "maintenance-tasks"} title={category === "unavailable" ? "核查与归档任务记录" : "维护任务与恢复"} actions={
    <Select aria-label="维护任务状态" value={status} onChange={event => { setStatus(event.target.value); setPage(0); }}>
      <option value="">全部状态</option>
      <option value="active">进行中／已暂停</option>
      <option value="done">已结束（含待确认预览）</option>
      <option value="cancelled">已取消</option>
    </Select>
  }>
    <p className="py-3 text-xs text-muted">任务在后台运行，关闭弹窗、切换页面不会停止。完成后点“查看预览”继续确认，无需重新核查；记录支持分页，不限于最近 30 条。</p>
    {result.error && <p className="pb-3 text-err">{result.error.message}</p>}
    {result.isPending && <p className="pb-3 text-muted">读取任务记录…</p>}
    <Table className="min-w-[54rem]">
      <thead><tr><Th>任务</Th><Th>范围</Th><Th>状态 / 结果</Th><Th>创建时间</Th><Th>操作</Th></tr></thead>
      <tbody>{result.data?.items.map(job => {
        const active = ["running", "paused"].includes(job.status);
        const preview = ["maintenance_preview", "maintenance_restore_preview"].includes(job.kind);
        const waiting = preview && job.status === "done" && job.state.ready && !job.state.partial_failure && !!job.state.eligible && !job.state.apply_job_id;
        const ids = job.operation_request?.scope === "global" ? [] : job.operation_request?.query.library_ids || job.params.library_ids;
        return <tr key={job.id}>
          <Td><b>#{job.id}</b> {job.name}</Td>
          <Td>{ids?.length ? ids.map(id => libraries.find(lib => lib.id === id)?.name || `库 #${id}`).join("、") : "全部配置库"}</Td>
          <Td>
            <span className={waiting ? "text-warn" : ""}>{waiting ? "预览已完成，待确认" : maintenanceStatus[job.status] || job.status}</span>
            {job.state.partial_failure && <span className="ml-2 text-err">有失败项</span>}
            {active && job.state.directories != null && <p className="text-xs text-muted">已读取 {fmtNum(job.state.directories)} 个目录</p>}
            {job.state.ready && preview && <p className="text-xs text-muted">可执行 {fmtNum(job.state.eligible || 0)} 组 · 跳过 {fmtNum(job.state.skipped || 0)} 组</p>}
            {job.state.apply_job_id && <p className="text-xs text-muted">已确认，执行任务 #{job.state.apply_job_id}</p>}
            {job.state.error && <p className="max-w-sm break-all text-xs text-err">{job.state.error}</p>}
          </Td>
          <Td>{fmtTime(job.created_at)}</Td>
          <Td><div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => job.kind === "maintenance_scan" ? navigate("jobs", { job: String(job.id) }) : onJob(job.id)}>
              {job.kind === "maintenance_scan" ? "任务详情" : active ? "继续查看" : preview ? "查看预览" : "查看结果"}
            </Button>
            {job.kind === "maintenance_apply" && !active && <Button size="sm" disabled={run.busy} onClick={() => restore(job)}>预览恢复</Button>}
          </div></Td>
        </tr>;
      })}</tbody>
    </Table>
    {!result.isPending && !result.error && !result.data?.items.length && <p className="py-4 text-center text-muted">暂无此状态的任务记录</p>}
    <div className="flex flex-wrap items-center gap-2 py-3">
      <Button size="sm" disabled={!page} onClick={() => setPage(page - 1)}>上一页</Button>
      <span className="text-xs text-muted">第 {page + 1} / {Math.max(1, Math.ceil((result.data?.total || 0) / 20))} 页 · 共 {fmtNum(result.data?.total || 0)} 条</span>
      <Button size="sm" disabled={!result.data || (page + 1) * 20 >= result.data.total} onClick={() => setPage(page + 1)}>下一页</Button>
      <Button size="sm" onClick={() => result.refetch()}>刷新任务记录</Button>
    </div>
  </Panel>;
}
