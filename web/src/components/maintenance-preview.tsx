import {
  maintenanceBytes as fmtBytes,
  maintenanceStatus,
} from "@/lib/maintenance";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { useRun } from "@/lib/queries";
import {
  itemStates,
  resourceNames,
  type MaintenanceItem,
  type MaintenanceJob,
} from "@/lib/maintenance";
import { ask } from "./confirm";
import { Button } from "./ui/button";
import { Mono, Table, Th, Td } from "./ui/data";
import { DialogContent } from "./ui/overlay";

function Files({ id }: { id: number }) {
  const [page, setPage] = useState(0);
  const result = useQuery({
    queryKey: ["maintenance-files", id, page],
    queryFn: () =>
      api.get<{
        items: {
          id: number;
          source: string;
          destination: string;
          kind: string;
          state: string;
          bytes: number;
          error: string;
        }[];
        total: number;
      }>(`/api/maintenance/items/${id}/files?offset=${page * 100}`),
  });
  return (
    <div className="my-2 space-y-2 rounded border border-line p-3">
      <p>完整文件清单 · {fmtNum(result.data?.total)} 项</p>
      {result.error && <p className="text-err">{result.error.message}</p>}
      {result.data?.items.map((f) => (
        <div key={f.id} className="break-all border-b border-line py-1 text-xs">
          <Mono>{f.source}</Mono> · {resourceNames[f.kind] || f.kind} ·{" "}
          {fmtBytes(f.bytes)} · {maintenanceStatus[f.state] || f.state}
          {f.destination && <p>归档／回收位置：{f.destination}</p>}
          {f.error && <p className="text-err">{f.error}</p>}
        </div>
      ))}
      <Button size="sm" disabled={!page} onClick={() => setPage(page - 1)}>
        上一页
      </Button>
      <Button
        size="sm"
        disabled={(page + 1) * 100 >= (result.data?.total || 0)}
        onClick={() => setPage(page + 1)}
      >
        下一页
      </Button>
    </div>
  );
}

export function MaintenancePreview({
  jobId,
  onJob,
  onClose,
}: {
  jobId: number;
  onJob: (id: number) => void;
  onClose: () => void;
}) {
  const run = useRun();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(0);
  const [files, setFiles] = useState<number>();
  const [busy, setBusy] = useState(false);
  const result = useQuery({
    queryKey: ["maintenance-job", jobId, page],
    queryFn: () =>
      api.get<{ job: MaintenanceJob; items: MaintenanceItem[]; total: number }>(
        `/api/maintenance/jobs/${jobId}?offset=${page * 50}`,
      ),
    refetchInterval: 1500,
  });
  const job = result.data?.job;
  const request = job?.params.request;
  const preview =
    job?.kind === "maintenance_preview" ||
    job?.kind === "maintenance_restore_preview";
  useEffect(() => {
    if (!job || preview || ["running", "paused"].includes(job.status)) return;
    for (const key of ["maintenance-jobs", "maintenance-overview", "maintenance-exclusions", "maintenance-assets", "libraries", "videos", "status"]) {
      void queryClient.invalidateQueries({ queryKey: [key] });
    }
  }, [job?.id, job?.status, preview, queryClient]);
  const restoring = job?.kind.includes("restore");
  const ready =
    preview &&
    job?.status === "done" &&
    job.state.ready &&
    !job.state.partial_failure;
  const apply = async () => {
    if (!job) return;
    if (
      !(await ask(
        restoring ? "确认恢复回收文件" : "确认执行媒体清理",
        <>
          <p>
            按当前固定预览处理 {fmtNum(job.state.eligible)}{" "}
            项。冲突或变化项跳过。
          </p>
          {request && (
            <>
              <p>
                {request.action === "resources"
                  ? `资源类型：${request.resource_types.map((k) => resourceNames[k]).join("、")}`
                  : request.action === "reconcile"
                    ? "仅清理缺失记录"
                    : request.action === "unavailable"
                      ? "归档无可用源影片及清退残留"
                    : "删除影片副本"}
                ；作用范围：
                {request.scope === "global" ? "所有库的当前影片版本" : "选定库"}
                。
              </p>
              {request.action !== "reconcile" && <p>
                {request.disposition === "archive"
                  ? "全部已确认文件移到库外归档目录，不自动过期。Emby 需刷新媒体库，且不得扫描归档目录。"
                  : request.disposition === "permanent" ||
                request.retention_days === 0
                  ? "文件永久删除，无法从本服务恢复。"
                  : `文件移进回收区，保留 ${request.retention_days} 天；不会立即释放磁盘空间。`}
              </p>}
              {request.action !== "resources" && (
                <p>
                  {request.stop_collection
                    ? "完成后禁止返回所选范围。"
                    : "允许以后重新补回。"}
                </p>
              )}
            </>
          )}
          <p>执行前请暂停目标目录的外部刮削写入。</p>
        </>,
        {
          danger: !restoring,
          confirmText: restoring ? "确认恢复" : "按预览执行",
        },
      ))
    )
      return;
    setBusy(true);
    try {
      await run(
        async () => {
          const r = await api.post<{ job_id: number }>(
            `/api/maintenance/jobs/${job.id}/apply`,
            { preview_token: job.state.preview_token },
          );
          setPage(0);
          onJob(r.job_id);
        },
        { invalidate: [["maintenance-jobs"], ["maintenance-overview"]] },
      );
    } finally {
      setBusy(false);
    }
  };
  const control = async (action: string) => {
    if (!job) return;
    await run(() => api.post(`/api/jobs/${job.id}/${action}`), {
      invalidate: [["maintenance-job"], ["maintenance-jobs"], ["jobs"]],
    });
  };
  return (
    <DialogContent
      title={`${restoring ? "回收恢复" : "媒体清理"} · 任务 #${jobId}`}
      className="max-w-5xl"
      footer={
        <>
          <Button disabled={busy} onClick={onClose}>{job && ["running", "paused"].includes(job.status) ? "关闭并稍后查看" : "关闭"}</Button>
          {job?.status === "running" && (
            <>
              <Button onClick={() => control("pause")}>暂停</Button>
              <Button onClick={() => control("cancel")}>取消后续项</Button>
            </>
          )}
          {job?.status === "paused" && (
            <Button onClick={() => control("resume")}>恢复执行</Button>
          )}
          {job &&
            !preview &&
            !["running", "paused"].includes(job.status) &&
            (job.state.failed || job.status === "cancelled") && (
              <Button
                onClick={() =>
                  run(() => api.post(`/api/maintenance/jobs/${job.id}/retry`), {
                    invalidate: [["maintenance-job"]],
                  })
                }
              >
                重试未完成项
              </Button>
            )}
          {job?.kind === "maintenance_apply" &&
            !["running", "paused"].includes(job.status) &&
            (job.state.failed || job.status === "cancelled") && (
              <Button
                onClick={async () => {
                  if (
                    await ask(
                      "放弃未完成项",
                      "已处理的文件不回滚。根据实际文件状态保留必要禁收记录，未处理的文件保持原样。",
                    )
                  )
                    await run(
                      () => api.post(`/api/maintenance/jobs/${job.id}/abandon`),
                      {
                        invalidate: [
                          ["maintenance-job"],
                          ["maintenance-exclusions"],
                        ],
                      },
                    );
                }}
              >
                放弃未完成项
              </Button>
            )}
          {ready && (
            <Button
              variant={restoring ? "primary" : "danger"}
              disabled={
                busy || !job?.state.eligible || !!job?.state.apply_job_id
              }
              onClick={apply}
            >
              {restoring ? "确认恢复" : "确认执行"}
            </Button>
          )}
          {job?.state.apply_job_id && (
            <Button onClick={() => onJob(job.state.apply_job_id!)}>
              查看执行结果
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-3 text-sm">
        {result.error && <p className="text-err">{result.error.message}</p>}
        <p>
          {job?.name} ·{" "}
          {job ? maintenanceStatus[job.status] || job.status : "读取中"}{" "}
          {job?.state.partial_failure && <b className="text-err">有失败项</b>}
        </p>
        <p className="text-xs text-muted">关闭弹窗不会停止后台任务。可从本页任务记录或通用任务详情重新打开；刷新页面会恢复当前任务。</p>
        {job?.state.error && <p className="text-err">{job.state.error}。本次核查未完成，不能执行，请处理后重新核查。</p>}
        {preview && request?.action === "unavailable" && job?.status === "running" && <p className="break-all text-muted">
          {job.state.scan_phase === "preparing" ? "正在建立本地影片索引" : job.state.scan_phase === "planning" ? "正在核对归档清单" : `正在核查 ${job.state.root || "目录"}`}
          {" "}· 已读取 {fmtNum(job.state.directories || 0)} 个目录；当前只读，不移动文件。
        </p>}
        {request && (
          <div className="rounded border border-line p-3">
            <p>
              资料：
              {request.query.source === "effective"
                ? "NFO 优先、源站补缺"
                : "可信 NFO"}
              ；
              {request.action === "resources"
                ? `资源清理（额外剧照保留 ${request.keep_extra} 张）`
                : request.action === "remove"
                  ? "整片清理"
                  : request.action === "unavailable"
                    ? "无可用源归档（按本地策略，未探测播放）"
                  : "缺失记录核对"}
              ；{request.scope === "global" ? "全局范围" : "指定库范围"}。
            </p>
            {request.action === "unavailable" && <>
              <p className="break-all">归档目录：{job?.state.archive_root || "核查中"}；不自动过期。保留有可用源影片 {fmtNum(job?.state.kept || 0)} 部；未识别目录 {fmtNum(job?.state.unknown || 0)} 个，不会处理。</p>
              <p>范围包括所选库的外部整理目录和收件区；确认前核对文件清单及所有跳过原因。</p>
            </>}
            <p>
              预览包含 {fmtNum(job?.state.eligible)} 个可执行组、
              {fmtNum(job?.state.files)} 个文件，
              {fmtBytes(job?.state.bytes || 0)}。跳过{" "}
              {fmtNum(job?.state.skipped || 0)} 组。
            </p>
            <details>
              <summary className="cursor-pointer">本次筛选条件</summary>
              <pre className="whitespace-pre-wrap break-all text-xs">
                {JSON.stringify(request.query, null, 2)}
              </pre>
            </details>
          </div>
        )}
        {!preview && (
          <p>
            完成 {fmtNum(job?.state.done || 0)} 组；失败{" "}
            {fmtNum(job?.state.failed || 0)}；归档／回收{" "}
            {fmtNum(job?.state.trashed_files || 0)} 个 /{" "}
            {fmtBytes(job?.state.trashed_bytes || 0)}；永久删除{" "}
            {fmtNum(job?.state.deleted_files || 0)} 个 /{" "}
            {fmtBytes(job?.state.deleted_bytes || 0)}；恢复{" "}
            {fmtNum(job?.state.restored || 0)} 组。
          </p>
        )}
        {!preview && job?.state.space && (
          <details>
            <summary className="cursor-pointer">
              文件系统可用空间观测（包含其他并发读写的影响）
            </summary>
            {Object.entries(job.state.space).map(([root, space]) => (
              <p className="break-all text-xs" key={root}>
                {root} · 操作前 {fmtBytes(space.before)} → 操作后{" "}
                {fmtBytes(space.after)}
              </p>
            ))}
          </details>
        )}
        <Table className="min-w-[42rem] table-fixed">
          <thead>
            <tr>
              <Th className="w-[30%]">影片 / 命中资料</Th>
              <Th className="w-[40%]">范围</Th>
              <Th className="w-[18%]">状态</Th>
              <Th className="w-[12%]">清单</Th>
            </tr>
          </thead>
          <tbody>
            {result.data?.items.map((item) => (
              <tr key={item.id}>
                <Td>
                  <b>{item.data.code || item.data.slug}</b>
                  <p>{item.data.title}</p>
                  {item.data.reason && <p className="text-xs text-warn">{item.data.reason}</p>}
                  {item.data.sources && <p className="text-xs text-muted">源：{item.data.sources.map(s => `${s.site} · ${s.status}${s.retired ? "（已清退）" : ""}`).join("；") || "无源记录"}</p>}
                  <p className="text-xs text-muted">
                    {item.data.metadata.release_date ||
                      item.data.metadata.year ||
                      "日期未知"}{" "}
                    · {item.data.metadata.categories?.join("、") || "类型未知"}
                  </p>
                  <p className="text-xs text-muted">
                    日期来源：
                    {item.data.field_sources.release_date === "nfo"
                      ? "NFO"
                      : "源站或未知"}{" "}
                    · 类型来源：
                    {item.data.field_sources.categories === "nfo"
                      ? "NFO"
                      : "源站或未知"}
                  </p>
                </Td>
                <Td>
                  {item.data.assets.map((a) => (
                    <p className="max-w-sm break-all text-xs" key={a.id}>
                      库 #{a.library_id} · {a.root}/{a.path}
                      {a.nfo_path && ` · NFO: ${a.nfo_path}`}
                    </p>
                  ))}
                </Td>
                <Td>
                  {itemStates[item.state] || item.state}
                  {item.error && (
                    <p className="max-w-xs text-xs text-err">{item.error}</p>
                  )}
                </Td>
                <Td>
                  <Button
                    size="sm"
                    onClick={() =>
                      setFiles(files === item.id ? undefined : item.id)
                    }
                  >
                    查看文件
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {files && <Files id={files} />}
        <div className="flex items-center gap-2">
          <Button disabled={!page} onClick={() => setPage(page - 1)}>
            上一页
          </Button>
          <span>
            {page + 1} /{" "}
            {Math.max(1, Math.ceil((result.data?.total || 0) / 50))}
          </span>
          <Button
            disabled={(page + 1) * 50 >= (result.data?.total || 0)}
            onClick={() => setPage(page + 1)}
          >
            下一页
          </Button>
        </div>
        <p className="text-xs text-muted">
          取消只停止后续项。清理前和清理中的资料、目录或文件变化会导致该组跳过或失败，不会自动扩大范围。清理后重新扫描更新空间统计。
        </p>
      </div>
    </DialogContent>
  );
}
