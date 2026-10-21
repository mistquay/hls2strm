import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ask } from "./confirm";
import { Button } from "./ui/button";
import { Input, Field } from "./ui/form";
import { TaskPreviewFrame } from "./task-preview-frame";
import { Mono, Table, Th, Td } from "./ui/data";
import { api, type Library } from "@/lib/api";
import { fmtBytes, fmtNum } from "@/lib/format";
import { useRun } from "@/lib/queries";
import { navigate } from "@/lib/route";

type Counts = Record<string, number>;
interface StorageJob {
  id: number;
  status: string;
  state: {
    files?: number;
    deleted?: number;
    deleted_bytes?: number;
    skipped?: number;
    failed?: number;
    preview_token?: string;
    apply_job_id?: number;
    summary?: {
      root: string;
      keep: number;
      counts: Counts;
      directories: ({path: string} & Counts)[];
      largest: {path: string; files: number; bytes: number}[];
    };
  };
}
const size = (n = 0) => n < 1024 ? `${n} B` : fmtBytes(n);

function StorageFiles({base, id}: {base: string; id: number}) {
  const [page, setPage] = useState(0);
  const files = useQuery({queryKey: ["storage-files", base, id, page], queryFn: ({signal}) => api.get<{items: {path: string; bytes: number}[]; total: number; available: boolean}>(`${base}/${id}/files?offset=${page * 100}`, signal)});
  return <div className="space-y-2">
    <p className="font-medium">原预览完整文件清单 · {fmtNum(files.data?.total || 0)} 张</p>
    {files.error && <p className="text-err">{files.error.message}</p>}
    {files.data && !files.data.available && <p className="text-warn">原清单未保存或已删除，不能还原；不会用最新一次扫描替代。</p>}
    <div className="max-h-72 space-y-1 overflow-auto">{files.data?.items.map(file => <p className="break-all text-xs" key={file.path}>{file.path} · {size(file.bytes)}</p>)}</div>
    <div className="flex gap-2"><Button size="sm" disabled={!page} onClick={() => setPage(page - 1)}>上一页</Button><span>{page + 1} / {Math.max(1, Math.ceil((files.data?.total || 0) / 100))}</span><Button size="sm" disabled={(page + 1) * 100 >= (files.data?.total || 0)} onClick={() => setPage(page + 1)}>下一页</Button></div>
  </div>;
}

export function LibraryStorage({lib, initialJobId, embedded = false}: {lib: Pick<Library, "id" | "name"> & Partial<Pick<Library, "root" | "external_root">>; initialJobId?: number; embedded?: boolean}) {
  const run = useRun();
  const [keep, setKeep] = useState("3");
  const [scanId, setScanId] = useState<number | undefined>(initialJobId);
  const [cleanId, setCleanId] = useState<number>();
  const [busy, setBusy] = useState(false);
  const base = `/api/libraries/${lib.id}/storage`;
  const latest = useQuery({queryKey: ["storage-latest", lib.id], queryFn: () => api.get<{job_id: number | null}>(base), enabled: !initialJobId});
  const id = scanId ?? latest.data?.job_id;
  const scan = useQuery({
    queryKey: ["storage", lib.id, id], enabled: !!id,
    queryFn: () => api.get<StorageJob>(`${base}/${id}`),
    refetchInterval: q => ["running", "paused"].includes(q.state.data?.status ?? "") ? 1500 : false,
  });
  const applyingId = cleanId ?? scan.data?.state.apply_job_id;
  const clean = useQuery({
    queryKey: ["storage-clean", lib.id, applyingId], enabled: !!applyingId,
    queryFn: () => api.get<StorageJob>(`${base}/${applyingId}`),
    refetchInterval: q => ["running", "paused"].includes(q.state.data?.status ?? "") ? 1500 : false,
  });
  const summary = scan.data?.state.summary;
  const counts = summary?.counts;
  const active = ["running", "paused"].includes(scan.data?.status || "") || ["running", "paused"].includes(clean.data?.status || "");
  const start = async () => {
    setBusy(true);
    try {
      await run(async () => {
        const r = await api.post<{job_id: number}>(base, {keep: Number(keep)});
        setScanId(r.job_id);
        setCleanId(undefined);
      }, {success: "已启动目录扫描", invalidate: [["jobs"], ["storage-latest", lib.id]]});
    } finally { setBusy(false); }
  };
  const apply = async () => {
    if (!summary || !scan.data || !id) return;
    if (!await ask("永久删除额外剧照", <>
      <p>目录：<Mono>{summary.root}</Mono></p>
      <p>{summary.keep === 0 ? "删除全部额外图片" : `每个 extrafanart 保留前 ${summary.keep} 张图片`}，预计删除 {fmtNum(counts?.delete_files)} 张，共 {size(counts?.delete_bytes)}。</p>
      <p>只删除预览清单内未改变的图片，不进入回收区，无法从本服务恢复。请暂停外部刮削工具后执行。</p>
    </>, {danger: true, confirmText: "确认永久删除"})) return;
    setBusy(true);
    try {
      await run(async () => {
        const r = await api.post<{job_id: number}>(`${base}/clean`, {preview_id: id, preview_token: scan.data.state.preview_token});
        setCleanId(r.job_id);
      }, {success: "清理任务已入队", invalidate: [["jobs"]]});
    } finally { setBusy(false); }
  };
  return <TaskPreviewFrame embedded={embedded} title={`目录占用与图片清理 · ${lib.name}`} className="max-w-3xl" footer={<>
    {!embedded && <Button disabled={busy || !!active || !keep.trim() || !Number.isInteger(Number(keep)) || Number(keep) < 0 || Number(keep) > 10000} onClick={start}>扫描并预览</Button>}
    <Button variant="danger" disabled={busy || !!active || !summary || !counts?.delete_files || !!applyingId || scan.data?.status !== "done"} onClick={apply}>按预览清理</Button>
    {!!id && !embedded && <Button onClick={() => navigate("jobs", {job: String(id)})}>查看此预览任务 #{id}</Button>}
    {!!applyingId && <Button onClick={() => navigate("jobs", {job: String(applyingId)})}>查看执行任务 #{applyingId}</Button>}
  </>}>
    <div className="space-y-4 text-sm">
      <p className="break-all">扫描目录：<Mono>{summary?.root || lib.external_root || lib.root || "读取原任务中"}</Mono></p>
      {!embedded && <Field label="每个 extrafanart 保留图片数（0 = 全部清理）">
        <Input type="number" min={0} max={10000} value={keep} onChange={e => setKeep(e.target.value)} />
      </Field>}
      <p className="text-xs text-muted">预览在后台运行，关闭弹窗不停止。任务详情按任务编号读取原清单，不会替换成最新一次扫描。</p>
      <p className="text-xs text-muted">按文件名自然顺序保留（fanart2 在 fanart10 前）。只清理 extrafanart 直接包含的图片，保留其他文件、子目录、主封面、NFO 和 STRM。跳过链接及 .hls2strm-trash 回收区。统计是文件逻辑大小，不含这些跳过项，实际释放空间取决于文件系统。</p>
      {(latest.error || scan.error || clean.error) && <p className="text-err">{(latest.error || scan.error || clean.error)?.message}</p>}
      {id && <p>扫描任务 #{id} · {scan.data?.status === "running" ? `扫描中，已检查 ${fmtNum(scan.data.state.files)} 个文件` : scan.data?.status === "done" && summary ? "扫描完成" : "结果未就绪；可在任务页查看、恢复或重试"}</p>}
      {summary && counts && <>
        <p>预览策略：{summary.keep === 0 ? "全部清理" : `每个目录保留前 ${summary.keep} 张`}。{embedded ? "这是原任务的策略和清单，不会使用最新扫描替代。" : "修改上面的数量后，需要重新扫描才会更新清理范围。"}</p>
        <div className="grid grid-cols-2 gap-3 rounded border border-line p-3 sm:grid-cols-3">
          <p>总占用 <b>{size(counts.bytes)}</b></p><p>额外剧照 <b>{size(counts.extra_bytes)}</b></p>
          <p>预计释放 <b>{size(counts.delete_bytes)}</b></p><p>其他图片 {size(counts.images_bytes)}</p>
          <p>NFO / STRM {size((counts.nfo_bytes || 0) + (counts.strm_bytes || 0))}</p><p>其他文件 {size(counts.other_bytes)}</p>
        </div>
        <p>{fmtNum(counts.files)} 个文件，计划删除 {fmtNum(counts.delete_files)} 张；跳过 {fmtNum(counts.skipped || 0)} 个链接、特殊文件或回收目录。</p>
        <p className="font-medium">一级目录占用（最多展示前 100 项）</p>
        <Table><thead><tr><Th>目录</Th><Th>总大小</Th><Th>额外剧照</Th><Th>可清理</Th></tr></thead>
          <tbody>{summary.directories.map(d => <tr key={d.path}><Td><span className="break-all">{d.path}</span></Td><Td>{size(d.bytes)}</Td><Td>{size(d.extra_bytes)}</Td><Td>{size(d.delete_bytes)}</Td></tr>)}</tbody>
        </Table>
        <details><summary className="cursor-pointer">清理目录示例（按可释放空间排序，最多 50 项）</summary>
          <ul className="mt-2 space-y-1">{summary.largest.map(d => <li className="break-all" key={d.path}>{d.path} · {d.files} 张 · {size(d.bytes)}</li>)}</ul>
        </details>
        {!!id && <StorageFiles base={base} id={id} />}
      </>}
      {applyingId && <div className="rounded border border-line p-3">
        <p>清理任务 #{applyingId} · {clean.data?.status === "running" ? "进行中" : clean.data?.status === "done" ? "已结束" : "请在任务页查看状态"}</p>
        <p>已删除 {fmtNum(clean.data?.state.deleted || 0)} 张 / {size(clean.data?.state.deleted_bytes)}；跳过 {fmtNum(clean.data?.state.skipped || 0)}，失败 {fmtNum(clean.data?.state.failed || 0)}。</p>
        <p className="text-xs text-muted">上面的占用为清理前快照；清理结束后重新扫描可查看剩余空间。任务支持在任务页暂停、恢复和取消。</p>
      </div>}
    </div>
  </TaskPreviewFrame>;
}
