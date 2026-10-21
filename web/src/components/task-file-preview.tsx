import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Job } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { maintenanceBytes as size } from "@/lib/maintenance";
import { useLibraries, useRun } from "@/lib/queries";
import { navigate } from "@/lib/route";
import { ask } from "./confirm";
import { LibraryStorage } from "./library-storage";
import { MaintenancePreview } from "./maintenance-preview";
import { OperationPreview, type OperationRequest } from "./operation-preview";
import { Button } from "./ui/button";
import { Table, Td, Th } from "./ui/data";

function TidyPreview({jobId}: {jobId: number}) {
  const run = useRun();
  const [page, setPage] = useState(0);
  const result = useQuery({
    queryKey: ["tidy-preview", jobId, page],
    queryFn: ({signal}) => api.get<{job: Job; preview_id: number; preview_token?: string; apply_job_id?: number; available: boolean; legacy: boolean;
      libraries: Record<string, {name: string; root: string; days: number; complete: boolean; error?: string}>;
      items: {library_id: number; library_name: string; path: string; bytes: number; state: string; destination?: string; permanent?: boolean; error?: string}[]; total: number}>(`/api/tidy/${jobId}?offset=${page * 50}`, signal),
    refetchInterval: query => ["running", "paused"].includes(query.state.data?.job.status || "") ? 1500 : false,
  });
  const data = result.data;
  const apply = async () => {
    if (!data || !await ask("按原检查清理残留目录", <>
      <p>只处理检查任务 #{data.preview_id} 的固定目录清单；新增或变化的目录不扩大清理。</p>
      {Object.values(data.libraries).map(info => <p className="break-all" key={info.root}>{info.name} · {info.root} · {info.days <= 0 ? "永久删除，不能恢复" : `移入回收区，保留 ${info.days} 天`}</p>)}
      <p>请暂停外部刮削写入。执行前检查挂载身份、目录和文件是否仍与预览一致。</p>
    </>, {danger: true, confirmText: "确认按原清单清理"})) return;
    const response = await run(() => api.post<{job_id: number}>(`/api/tidy/${data.preview_id}/apply`, {preview_token: data.preview_token}), {invalidate: [["jobs"], ["tidy-preview"]]});
    if (response) navigate("jobs", {job: String(response.job_id)});
  };
  const active = data && ["running", "paused"].includes(data.job.status);
  const canApply = data?.job.params.apply === false && data.job.status === "done" && !!data.preview_token && data.available
    && !data.job.tasks.failed && !data.job.tasks.cancelled && Object.values(data.libraries).every(info => info.complete);
  return <section className="space-y-3 rounded border border-line p-3" aria-label="残留目录检查预览">
    <h4 className="font-semibold">残留目录检查预览与结果</h4>
    <p className="text-xs text-muted">完整目录清单保存在任务中，不依赖日志或原弹窗。关闭页面不停止任务；按原清单确认时会重新核对文件，不清理后来出现的目录。</p>
    {result.error && <p className="text-err">{result.error.message}</p>}
    {!data && !result.error && <p>读取原检查结果…</p>}
    {!!data?.job.tasks.failed && <p className="text-warn">部分库检查失败，不能按这次检查清理；请查看失败子任务并重新检查。</p>}
    {data && Object.values(data.libraries).filter(info => info.error).map(info => <p className="break-all text-warn" key={info.root}>{info.name} · {info.root}：{info.error}</p>)}
    {data && !data.available && <p className="text-warn">{active ? "正在检查，完整清单生成后会显示。" : data.legacy ? "此旧任务没有保存完整清单，无法从日志准确还原；请重新执行一次只检查，不会自动重新扫描或清理。" : "完整清单尚未生成或已丢失，请查看失败子任务；不会自动重新扫描或扩大清理范围。"}</p>}
    {data?.available && <>
      <p>目录 {fmtNum(data.total)} 个 · {size(Number(data.job.state.bytes || 0))} · 移走 {fmtNum(Number(data.job.state.moved || 0))} · 跳过 {fmtNum(Number(data.job.state.skipped || 0))} · 失败 {fmtNum(Number(data.job.state.failed || 0))}</p>
      <Table className="min-w-[34rem]"><thead><tr><Th>输出库 / 原目录</Th><Th>大小</Th><Th>状态 / 目标</Th></tr></thead><tbody>
        {data.items.map(item => <tr key={`${item.library_id}:${item.path}`}><Td><b>{item.library_name}</b><p className="max-w-lg break-all text-xs">{item.path}</p></Td><Td>{size(item.bytes)}</Td><Td>
          {item.state === "moved" && item.permanent ? "已永久删除" : ({checked: "检查命中", moved: "已移入回收区", skipped: "已跳过", failed: "失败"} as Record<string, string>)[item.state] || item.state}
          {item.destination && <p className="max-w-sm break-all text-xs">{item.destination}</p>}
          {item.error && <p className="max-w-sm break-all text-xs text-warn">{item.error}</p>}
        </Td></tr>)}
      </tbody></Table>
      <div className="flex flex-wrap gap-2"><Button size="sm" disabled={!page} onClick={() => setPage(page - 1)}>上一页</Button><span>{page + 1} / {Math.max(1, Math.ceil(data.total / 50))}</span><Button size="sm" disabled={(page + 1) * 50 >= data.total} onClick={() => setPage(page + 1)}>下一页</Button></div>
    </>}
    {canApply && !data.apply_job_id && <Button variant="danger" disabled={run.busy || !data.total} onClick={apply}>按检查清单清理</Button>}
    {data?.apply_job_id && data.apply_job_id !== jobId && <Button onClick={() => navigate("jobs", {job: String(data.apply_job_id)})}>查看清理任务 #{data.apply_job_id}</Button>}
  </section>;
}

function PrefixChanges({job}: {job: Job}) {
  const [page, setPage] = useState(0);
  const id = job.kind === "revert" ? Number(job.params.change_set) : job.id;
  const result = useQuery({queryKey: ["prefix-files", id, job.status, page], queryFn: ({signal}) => api.get<{items: {id: number; path: string; old: string; new: string; reverted: number}[]; total: number}>(`/api/strm/changes/${id}?offset=${page * 50}`, signal), refetchInterval: ["running", "paused"].includes(job.status) ? 1500 : false});
  return <section className="space-y-3 rounded border border-line p-3"><h4 className="font-semibold">改前缀的原内容与变更记录 · {fmtNum(result.data?.total || 0)} 个文件</h4>
    <p className="text-xs text-muted">展示实际记录的改动，不用当前文件或最新扫描替代历史结果。回滚任务查看同一批原始改动及其恢复状态。</p>
    {result.error && <p className="text-err">{result.error.message}</p>}
    {result.data?.items.map(file => <div className="space-y-1 rounded bg-panel-2 p-2 text-xs" key={file.id}><b className="break-all">{file.path}</b> · {file.reverted ? "已回滚" : "已修改"}<pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all">原内容：{file.old}</pre><pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all">修改后：{file.new}</pre></div>)}
    {!result.isPending && !result.data?.total && <p className="text-muted">没有已记录的文件改动；任务可能尚未执行到文件或已跳过全部文件。</p>}
    <div className="flex gap-2"><Button size="sm" disabled={!page} onClick={() => setPage(page - 1)}>上一页</Button><span>{page + 1} / {Math.max(1, Math.ceil((result.data?.total || 0) / 50))}</span><Button size="sm" disabled={(page + 1) * 50 >= (result.data?.total || 0)} onClick={() => setPage(page + 1)}>下一页</Button></div>
  </section>;
}

export function TaskFilePreview({job}: {job: Job}) {
  const {data: libraries = []} = useLibraries(true);
  const previewId = Number(job.params.preview_id || 0);
  const own = job.kind === "preview" || job.kind === "tidy" || job.kind.startsWith("maintenance_") || job.kind.startsWith("storage_");
  const source = useQuery<Job>({queryKey: ["jobs", "preview-source", previewId], queryFn: ({signal}) => api.get<Job>(`/api/jobs/${previewId}?summary=true`, signal), enabled: !own && previewId > 0 && previewId !== job.id});
  const operation = job.kind === "preview" ? job : source.data?.kind === "preview" ? source.data : undefined;
  const libraryId = Number(job.params.library_id);
  const storageId = job.kind === "storage_scan" ? job.id : previewId;
  return <>
    {previewId > 0 && previewId !== job.id && <Button onClick={() => navigate("jobs", {job: String(previewId)})}>查看原预览任务 #{previewId}</Button>}
    {source.error && <p className="text-warn">原预览无法读取：{source.error.message}</p>}
    {operation?.params.operation != null && <OperationPreview key={operation.id} request={operation.params.operation as unknown as OperationRequest} initialJobId={operation.id} onApplied={id => navigate("jobs", {job: String(id)})} />}
    {job.kind.startsWith("maintenance_") && job.kind !== "maintenance_scan" && <MaintenancePreview embedded jobId={job.id} onJob={id => navigate("jobs", {job: String(id)})} />}
    {job.kind === "tidy" && <TidyPreview key={job.id} jobId={job.id} />}
    {["storage_scan", "storage_clean"].includes(job.kind) && (libraryId > 0 && storageId > 0
      ? <LibraryStorage key={job.id} embedded lib={{id: libraryId, name: libraries.find(lib => lib.id === libraryId)?.name || `库 #${libraryId}`}} initialJobId={storageId} />
      : <p className="text-warn">原任务缺少库编号或预览编号，不能还原原清单；不会用最新一次扫描替代。</p>)}
    {["prefix", "revert"].includes(job.kind) && <PrefixChanges key={job.id} job={job} />}
  </>;
}
