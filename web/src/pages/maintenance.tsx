import { MaintenanceFilters } from "@/components/maintenance-filters";
import { MaintenanceOptions } from "@/components/maintenance-options";
import { MaintenanceAssets } from "@/components/maintenance-assets";
import { MaintenanceSpace, type Root } from "@/components/maintenance-space";
import { MaintenanceScan } from "@/components/maintenance-scan";
import { MaintenanceJobs } from "@/components/maintenance-jobs";
import { MetadataFallback } from "@/components/metadata-fallback";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useLibraries, useRun } from "@/lib/queries";
import { replaceParams, useRoute } from "@/lib/route";
import {
  type Asset,
  type AssetFilter,
  type CleanupConfig,
  type MaintenanceJob,
} from "@/lib/maintenance";
import { ask } from "@/components/confirm";
import { AssetBinding } from "@/components/local-metadata";
import { ResourcePolicies } from "@/components/resource-policies";
import { MaintenancePreview } from "@/components/maintenance-preview";
import { Button } from "@/components/ui/button";
import { Check, Select } from "@/components/ui/form";
import { Panel, Table, Td, Th } from "@/components/ui/data";
import { Dialog } from "@/components/ui/overlay";

interface Exclusion {
  video_id: number;
  library_id: number;
  job_id: number;
  code: string;
  title: string;
  reason: string;
  created_at: number;
  library_name?: string;
}
export default function Maintenance() {
  const route = useRoute();
  const run = useRun();
  const queryClient = useQueryClient();
  const { data: libs = [] } = useLibraries(true);
  const tab = route.params.get("tab") || "assets";
  const view = ["space", "assets", "cleanup", "differences", "unavailable", "history", "metadata"].includes(tab) ? tab : "assets";
  const setView = (value: string) => {
    const params = new URLSearchParams(route.params);
    params.set("tab", value);
    params.delete("job");
    replaceParams(params);
  };
  const [query, setQuery] = useState<AssetFilter>(() => {
    let video: Record<string, unknown> = {};
    try {
      video = JSON.parse(route.params.get("filter") || "{}");
    } catch {
      /* malformed link uses visible defaults */
    }
    return {
      source: "nfo",
      library_ids: route.params.get("library")
        ? [Number(route.params.get("library"))]
        : video.library_id
          ? [Number(video.library_id)]
          : [],
      video_ids: (route.params.get("ids") || "")
        .split(",")
        .filter(Boolean)
        .map(Number),
      video,
      sort: "bytes",
    };
  });
  const [page, setPage] = useState(0);
  const [selection, setSelection] = useState<Set<number>>(new Set());
  const [all, setAll] = useState(false);
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const requestedJob = Number(route.params.get("job"));
  const jobId = Number.isSafeInteger(requestedJob) && requestedJob > 0 ? requestedJob : undefined;
  const setJobId = (id?: number) => {
    const params = new URLSearchParams(route.params);
    params.set("tab", view);
    if (id) params.set("job", String(id));
    else params.delete("job");
    replaceParams(params);
  };
  const [busy, setBusy] = useState(false);
  const [scanSubmitting, setScanSubmitting] = useState(false);
  const [binding, setBinding] = useState<Asset>();
  const [cleanup, setCleanup] = useState<Omit<CleanupConfig, "query">>({
    action: "resources",
    scope: "library",
    resource_types: ["extra"],
    keep_extra: 3,
    disposition: "permanent",
    retention_days: 7,
    stop_collection: true,
    allow_empty: false,
  });
  const signature = JSON.stringify(query);
  useEffect(() => {
    setPage(0);
    setSelection(new Set());
    setAll(false);
    setExcluded(new Set());
  }, [signature]);
  const overview = useQuery({
    queryKey: ["maintenance-overview"],
    queryFn: () =>
      api.get<{
        roots: Root[];
        groups: {
          library_id: number;
          root: string;
          directory: string;
          bytes: number;
          extra_bytes: number;
          assets: number;
        }[];
        archive_root: string;
        jobs: MaintenanceJob[];
      }>("/api/maintenance/overview"),
    refetchInterval: 3000,
  });
  const assets = useQuery({
    queryKey: ["maintenance-assets", signature, page],
    queryFn: () =>
      api.post<{
        items: Asset[];
        total: number;
        bytes: number;
        extra_bytes: number;
      }>(`/api/maintenance/assets/query?offset=${page * 50}`, query),
    enabled: !["history", "space", "unavailable", "metadata"].includes(view),
  });
  const completedScan = overview.data?.jobs.find((job) => job.kind === "maintenance_scan" && job.status === "done")?.id;
  useEffect(() => {
    if (!completedScan) return;
    void queryClient.invalidateQueries({ queryKey: ["maintenance-assets"] });
    setSelection(new Set());
    setAll(false);
    setExcluded(new Set());
  }, [completedScan, queryClient]);
  const bans = useQuery({
    queryKey: ["maintenance-exclusions", page],
    queryFn: () =>
      api.get<{ items: Exclusion[]; total: number }>(
        `/api/maintenance/exclusions?offset=${page * 50}`,
      ),
    enabled: view === "history",
  });
  const update = (values: Partial<AssetFilter>) =>
    setQuery((q) => ({ ...q, ...values }));
  const toggle = (id: number) => {
    const change = (previous: Set<number>) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    };
    if (all) setExcluded(change);
    else setSelection(change);
  };
  const chosen = (id: number) => (all ? !excluded.has(id) : selection.has(id));
  const refresh = {
    invalidate: [
      ["maintenance-assets"],
      ["maintenance-overview"],
      ["maintenance-jobs"],
      ["maintenance-exclusions"],
      ["libraries"],
    ],
  };
  const scan = async (force = false) => {
    setScanSubmitting(true);
    try { await run(
      () =>
        api.post<{ job_id: number }>("/api/maintenance/scan", {
          library_ids: query.library_ids,
          force,
        }),
      { success: (r) => `扫描任务 #${r.job_id} 已提交（相同范围的已有任务会复用）`, ...refresh },
    ); } finally { setScanSubmitting(false); }
  };
  const preview = async () => {
    if (!all && !selection.size) return;
    setBusy(true);
    try {
      await run(async () => {
        const r = await api.post<{ job_id: number }>(
          "/api/maintenance/preview",
          {
            ...cleanup,
            query: {
              ...query,
              asset_ids: all ? [] : [...selection],
              exclude_ids: all ? [...excluded] : [],
            },
          },
        );
        setJobId(r.job_id);
      }, refresh);
    } finally {
      setBusy(false);
    }
  };
  const unavailablePreview = async () => {
    setBusy(true);
    try {
      await run(async () => {
        const result = await api.post<{ job_id: number }>("/api/maintenance/preview", {
          action: "unavailable", scope: "library", disposition: "archive", stop_collection: true,
          query: { library_ids: query.library_ids, video_ids: query.video_ids },
        });
        setJobId(result.job_id);
      }, refresh);
    } finally {
      setBusy(false);
    }
  };
  const policy = async (id: number, value: string) => {
    if (
      !(await ask(
        "修改缺失文件处理策略",
        value === "review"
          ? "成功扫描建立基线后，发现缺失先等待确认，不再自动补回。"
          : "恢复现有补回策略，真正缺失的影片可能重新写入收件目录。",
      ))
    )
      return;
    await run(
      () =>
        api.put(`/api/maintenance/libraries/${id}/policy`, {
          missing_policy: value,
        }),
      { success: "策略已保存", ...refresh },
    );
  };
  const selectedCount = all
    ? Math.max(0, (assets.data?.total || 0) - excluded.size)
    : selection.size;
  const activeScan = overview.data?.jobs.some((j) => j.kind === "maintenance_scan" && ["running", "paused"].includes(j.status));
  if (view === "metadata") return <MetadataFallback />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {[
          ["space", "空间概览"],
          ["assets", "本地资料与筛选"],
          ["cleanup", "筛选清理"],
          ["differences", "磁盘差异"],
          ["unavailable", "无可用源／清退残留"],
          ["history", "记录与恢复"],
          ["metadata", "源站资料补全"],
        ].map(([key, label]) => (
          <Button
            key={key}
            variant={view === key ? "primary" : "outline"}
            onClick={() => {
              setView(key);
              setPage(0);
              if (key === "differences") {
                update({ status: "missing" });
                setCleanup((c) => ({ ...c, action: "reconcile" }));
              } else if (view === "differences") update({ status: "" });
            }}
          >
            {label}
          </Button>
        ))}
      </div>
      <Panel
        title="维护范围"
        actions={
          <>
            {view !== "unavailable" && <>
              <Button disabled={scanSubmitting || activeScan} onClick={() => scan()}>扫描磁盘与 NFO</Button>
              <Button disabled={scanSubmitting || activeScan} onClick={() => scan(true)}>强制重读 NFO</Button>
            </>}
            <Button onClick={() => run(async () => {}, refresh)}>
              刷新结果
            </Button>
          </>
        }
      >
        <div className="flex flex-wrap gap-4 py-3">
          {libs.map((lib) => (
            <Check
              key={lib.id}
              checked={query.library_ids?.includes(lib.id) || false}
              onChange={(checked) =>
                update({
                  library_ids: checked
                    ? [...(query.library_ids || []), lib.id]
                    : (query.library_ids || []).filter((id) => id !== lib.id),
                })
              }
            >
              {lib.name}
            </Check>
          ))}
        </div>
        <p className="pb-3 text-xs text-muted">
          {query.library_ids?.length
            ? `已选择 ${query.library_ids.length} 个库`
            : "未限定库：扫描和筛选全部配置库"}
          {view === "unavailable"
            ? "。本次直接核查外部整理目录及收件目录，不依赖旧扫描或输出记录。"
            : "。已配置外部整理目录时只扫描外部整理目录，未配置才扫描输出目录。NFO 读取不改写文件；源站资料与本地资料分别保留。扫描完成后自动刷新结果。"}
        </p>
        <div className="space-y-1 pb-3 text-xs text-muted">
          {libs.filter((lib) => !query.library_ids?.length || query.library_ids.includes(lib.id)).map((lib) => (
            <p key={lib.id} className="break-all">{lib.name} · {lib.external_root ? "外部整理目录" : "输出目录"}：{lib.external_root || lib.root}</p>
          ))}
        </div>
        <MaintenanceScan jobs={overview.data?.jobs || []} />
      </Panel>
      {(overview.error || assets.error || bans.error) && (
        <p className="text-err">
          {(overview.error || assets.error || bans.error)?.message}
        </p>
      )}
      {view === "space" && (
        <MaintenanceSpace overview={overview} query={query} libs={libs} />
      )}
      {view === "unavailable" && <><Panel title="无可用源影片与清退残留核查">
        <div className="space-y-3 py-3 text-sm">
          <p>只看本地源状态、站点启停及支持的线路，不请求源站、不探测播放。站点停用、线路全停用也计为无可用源，不仅限于已清退站点；“本地可用”不等于已经验证能播放。</p>
          <p>按 STRM 内容识别已改名、移动或失去输出记录的影片；只剩 NFO 的目录需能唯一关联影片，普通版与其他版本有歧义时跳过。</p>
          <p>确认后将独占影片目录内全部文件（STRM、NFO、图片、字幕、预告片等）移到库外归档目录，停止所选库重新收录。平铺收件区仅归档明确属于影片的 STRM；共享整理目录、身份冲突、链接和实体视频保留并说明原因。</p>
          <p className="break-all">归档目录：{overview.data?.archive_root || "读取中"}。归档不自动过期，不是永久删除，也不保证释放源磁盘空间；请确保 Emby 没有扫描此目录。</p>
          <p className="text-warn">预览只读取文件并保存清单；确认执行前请暂停外部刮削写入。文件移出后仍需 Emby 刷新媒体库，本功能不调用 Emby API。</p>
          <p>提交后可关闭弹窗，任务继续后台运行。完成后从下方任务记录打开原预览，无需一直等待或重新扫描；同范围核查未结束时再次点击会打开原任务。</p>
          {!!query.video_ids?.length && <p>仅核查从影片库带入的 {query.video_ids.length} 部影片。</p>}
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" disabled={busy || activeScan || !!overview.error} onClick={unavailablePreview}>
              {busy ? "提交中…" : "只读核查并生成归档预览"}
            </Button>
            <Button onClick={() => document.getElementById("unavailable-tasks")?.scrollIntoView({ behavior: "smooth", block: "start" })}>查看任务记录</Button>
          </div>
        </div>
      </Panel><MaintenanceJobs category="unavailable" onJob={setJobId} /></>}
      {!["space", "history", "unavailable"].includes(view) && (
        <>
          <MaintenanceFilters query={query} update={update} />
          {view === "differences" && (
            <Panel title="缺失处理策略">
              <div className="space-y-2 py-3">
                {libs
                  .filter(
                    (l) =>
                      !query.library_ids?.length ||
                      query.library_ids.includes(l.id),
                  )
                  .map((lib) => (
                    <div className="flex items-center gap-3" key={lib.id}>
                      <span>{lib.name}</span>
                      <Select
                        value={lib.missing_policy || "restore"}
                        onChange={(e) => policy(lib.id, e.target.value)}
                      >
                        <option value="restore">按现有设置补回</option>
                        <option value="review">缺失先等待确认</option>
                      </Select>
                    </div>
                  ))}
                <p className="text-xs text-muted">
                  未成功扫描、挂载异常、整库突然为空都不会自动退库。影片改名或移动按
                  STRM 内容找回。
                </p>
              </div>
            </Panel>
          )}
          <MaintenanceAssets
            assets={assets}
            libs={libs}
            selectedCount={selectedCount}
            chosen={chosen}
            toggle={toggle}
            setBinding={setBinding}
            page={page}
            setPage={setPage}
            selectAll={() => {
              setAll(true);
              setExcluded(new Set());
            }}
            clearSelection={() => {
              setAll(false);
              setSelection(new Set());
              setExcluded(new Set());
            }}
          />
          <MaintenanceOptions
            cleanup={cleanup}
            setCleanup={setCleanup}
            busy={busy}
            selectedCount={selectedCount}
            preview={preview}
          />
        </>
      )}
      {view === "history" && (
        <>
          <MaintenanceJobs onJob={setJobId} />
          <Panel title="已停止收录">
            <Table>
              <thead>
                <tr>
                  <Th>影片</Th>
                  <Th>排除范围</Th>
                  <Th>原因</Th>
                  <Th>操作</Th>
                </tr>
              </thead>
              <tbody>
                {bans.data?.items.map((b) => (
                  <tr key={`${b.video_id}:${b.library_id}:${b.job_id}`}>
                    <Td>
                      {b.code}
                      <p>{b.title}</p>
                    </Td>
                    <Td>
                      {b.library_id
                        ? b.library_name || `库 #${b.library_id}`
                        : "所有库"}
                    </Td>
                    <Td>
                      {b.reason} · 任务 #{b.job_id}
                    </Td>
                    <Td>
                      <Button
                        size="sm"
                        onClick={async () => {
                          if (
                            await ask(
                              "允许重新收录",
                              `解除 ${b.code} 的这条排除记录；不会恢复已永久删除的文件。`,
                            )
                          )
                            await run(
                              () =>
                                api.del(
                                  `/api/maintenance/exclusions/${b.video_id}/${b.library_id}/${b.job_id}`,
                                ),
                              refresh,
                            );
                        }}
                      >
                        解除本条排除
                      </Button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <div className="flex gap-2 py-3">
              <Button disabled={!page} onClick={() => setPage(page - 1)}>
                上一页
              </Button>
              <Button
                disabled={(page + 1) * 50 >= (bans.data?.total || 0)}
                onClick={() => setPage(page + 1)}
              >
                下一页
              </Button>
            </div>
          </Panel>
          <ResourcePolicies />
        </>
      )}
      <Dialog
        open={!!binding}
        onOpenChange={(open) => !open && setBinding(undefined)}
      >
        {binding && (
          <AssetBinding asset={binding} onDone={() => setBinding(undefined)} />
        )}
      </Dialog>
      <Dialog
        open={!!jobId}
        onOpenChange={(open) => !open && setJobId(undefined)}
      >
        {jobId && (
          <MaintenancePreview key={jobId} jobId={jobId} onJob={setJobId} onClose={() => setJobId(undefined)} />
        )}
      </Dialog>
    </div>
  );
}
