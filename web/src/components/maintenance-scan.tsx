import { maintenanceBytes, maintenanceStatus, scanPhases, type MaintenanceJob } from "@/lib/maintenance";
import { fmtTime } from "@/lib/format";
import { navigate } from "@/lib/route";
import { Button } from "@/components/ui/button";

export function MaintenanceScan({ jobs }: { jobs: MaintenanceJob[] }) {
  const scans = jobs.filter((job) => job.kind === "maintenance_scan");
  const active = scans.filter((job) => ["running", "paused"].includes(job.status));
  const visible = active.length ? active : scans.slice(0, 1);
  return <div className="space-y-3 pb-3">
    {visible.map((job) => {
      const state = job.state;
      const phase = state.scan_phase ? scanPhases[state.scan_phase] || state.scan_phase : "扫描进行中";
      return <div key={job.id} className="rounded border border-border p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>扫描 #{job.id} · {job.status === "running" ? phase : maintenanceStatus[job.status] || job.status}
            {state.library_name && ` · ${state.library_name}（${state.library_index}/${state.library_total}）`}
          </span>
          <Button size="sm" onClick={() => navigate("jobs", { q: String(job.id) })}>{["running", "paused"].includes(job.status) ? "任务详情 / 暂停 / 取消" : "任务详情"}</Button>
        </div>
        {state.root && <p className="mt-2 break-all">目录：{state.root}</p>}
        {state.current_path && <p className="break-all text-xs text-muted">当前位置：{state.current_path}</p>}
        <p className="mt-2">已扫描 {(state.assets || 0).toLocaleString()} 个目录 · {(state.files || 0).toLocaleString()} 个文件 · {maintenanceBytes(state.bytes || 0)} · 额外剧照 {maintenanceBytes(state.extra_bytes || 0)}</p>
        {!!state.outputs_checked && <p>已核对 {state.outputs_checked.toLocaleString()} 条输出记录</p>}
        {state.updated_at && <p className="text-xs text-muted">最近进度：{fmtTime(state.updated_at)} · 本轮已用时 {state.elapsed_seconds || 0} 秒</p>}
        {!!state.failed_roots && <p className="text-err">{state.failed_roots} 个目录扫描失败，结果不完整。</p>}
        {state.root_errors?.map((error) => <p key={error.root} className="break-all text-xs text-err">{error.root}：{error.error}</p>)}
        <p className="mt-1 text-xs text-muted">完整扫描后发布本地资料；扫描期间保留上次结果。</p>
      </div>;
    })}
  </div>;
}
