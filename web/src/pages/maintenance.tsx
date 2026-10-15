import { MaintenanceFilters } from "@/components/maintenance-filters";
import { MaintenanceOptions } from "@/components/maintenance-options";
import { MaintenanceAssets } from "@/components/maintenance-assets";
import { MaintenanceSpace, type Root } from "@/components/maintenance-space";
import { maintenanceStatus } from "@/lib/maintenance";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useLibraries, useRun } from "@/lib/queries";
import { navigate, useRoute } from "@/lib/route";
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
  const { data: libs = [] } = useLibraries(true);
  const [view, setView] = useState("assets");
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
  const [jobId, setJobId] = useState<number>();
  const [busy, setBusy] = useState(false);
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
    enabled: view !== "history" && view !== "space",
  });
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
      ["maintenance-exclusions"],
      ["libraries"],
    ],
  };
  const scan = async (force = false) => {
    await run(
      () =>
        api.post<{ job_id: number }>("/api/maintenance/scan", {
          library_ids: query.library_ids,
          force,
        }),
      { success: (r) => `扫描任务 #${r.job_id} 已入队`, ...refresh },
    );
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
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {[
          ["space", "空间概览"],
          ["assets", "本地资料与筛选"],
          ["cleanup", "筛选清理"],
          ["differences", "磁盘差异"],
          ["history", "记录与恢复"],
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
            <Button onClick={() => scan()}>扫描磁盘与 NFO</Button>
            <Button onClick={() => scan(true)}>强制重读 NFO</Button>
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
          。NFO
          读取不改写文件；源站资料与本地资料分别保留。扫描完成后点击“刷新结果”。
        </p>
        {overview.data?.jobs.some(
          (j) => j.kind === "maintenance_scan" && j.status === "running",
        ) && (
          <p className="pb-3 text-accent">扫描进行中；任务页可暂停或取消。</p>
        )}
      </Panel>
      {(overview.error || assets.error || bans.error) && (
        <p className="text-err">
          {(overview.error || assets.error || bans.error)?.message}
        </p>
      )}
      {view === "space" && (
        <MaintenanceSpace overview={overview} query={query} libs={libs} />
      )}
      {view !== "space" && view !== "history" && (
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
          <Panel title="维护任务与恢复">
            <Table>
              <thead>
                <tr>
                  <Th>任务</Th>
                  <Th>状态</Th>
                  <Th>时间</Th>
                  <Th>操作</Th>
                </tr>
              </thead>
              <tbody>
                {overview.data?.jobs.map((job) => (
                  <tr key={job.id}>
                    <Td>
                      #{job.id} {job.name}
                    </Td>
                    <Td>
                      {maintenanceStatus[job.status] || job.status}
                      {job.state.partial_failure && (
                        <b className="ml-2 text-err">有失败项</b>
                      )}
                    </Td>
                    <Td>{fmtTime(job.created_at)}</Td>
                    <Td>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          onClick={() =>
                            job.kind === "maintenance_scan"
                              ? navigate("jobs", { q: String(job.id) })
                              : setJobId(job.id)
                          }
                        >
                          查看
                        </Button>
                        {job.kind === "maintenance_apply" &&
                          !["running", "paused"].includes(job.status) && (
                            <Button
                              size="sm"
                              onClick={() =>
                                run(async () => {
                                  const r = await api.post<{ job_id: number }>(
                                    `/api/maintenance/jobs/${job.id}/restore-preview`,
                                  );
                                  setJobId(r.job_id);
                                }, refresh)
                              }
                            >
                              预览恢复
                            </Button>
                          )}
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>
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
          <MaintenancePreview key={jobId} jobId={jobId} onJob={setJobId} />
        )}
      </Dialog>
    </div>
  );
}
