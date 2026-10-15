import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Video } from "@/lib/api";
import { type Asset, type LocalMetadata, nfoStates } from "@/lib/maintenance";
import { useRun } from "@/lib/queries";
import { ask } from "./confirm";
import { Button } from "./ui/button";
import { Field, Input } from "./ui/form";
import { DialogContent } from "./ui/overlay";

export function LocalMetadataPanel({ videoId }: { videoId: number }) {
  const q = useQuery({
    queryKey: ["local-metadata", videoId],
    queryFn: () =>
      api.get<{
        items: Asset[];
        exclusions: { library_id: number; reason: string }[];
      }>(`/api/maintenance/videos/${videoId}`),
  });
  if (!q.data?.items.length && !q.data?.exclusions.length && !q.error)
    return null;
  return (
    <section className="space-y-2 rounded border border-line p-3 text-sm">
      <b>本地 NFO 资料</b>
      {q.error && <p className="text-err">{q.error.message}</p>}
      {q.data?.exclusions.map((e, i) => (
        <p className="text-warn" key={i}>
          {e.library_id ? `库 #${e.library_id}` : "所有库"} 已停止收录：
          {e.reason}
        </p>
      ))}
      {q.data?.items.map((a) => (
        <div key={a.id} className="border-t border-line pt-2">
          <p>
            库 #{a.library_id} · NFO {nfoStates[a.nfo_status]}
          </p>
          <p>
            {a.metadata.title || a.effective.title || "无本地标题"} ·{" "}
            {a.metadata.release_date || a.metadata.year || "发行日期未知"}
          </p>
          <p>
            类型：{a.metadata.categories?.join("、") || "未知"}；演员：
            {a.metadata.models?.join("、") || "未知"}
          </p>
          <p>
            厂牌：{a.metadata.maker || "未知"}；时长：
            {a.metadata.duration
              ? `${Math.round(a.metadata.duration / 60)} 分钟`
              : "未知"}
          </p>
          <p className="break-all text-xs text-muted">
            {a.root}/{a.path}/{a.details.nfo_path || ""}
          </p>
          {a.details.nfo_error && (
            <p className="text-err">{a.details.nfo_error}</p>
          )}
          {a.details.historical_metadata && (
            <p className="text-muted">
              最近成功资料：{a.details.historical_metadata.title} ·{" "}
              {a.details.historical_metadata.release_date ||
                a.details.historical_metadata.year}
              （历史快照）
            </p>
          )}
        </div>
      ))}
    </section>
  );
}

export function AssetBinding({
  asset,
  onDone,
}: {
  asset: Asset;
  onDone: () => void;
}) {
  const [q, setQ] = useState(asset.metadata.code || "");
  const run = useRun();
  const videos = useQuery({
    queryKey: ["bind-videos", q],
    queryFn: () =>
      api.get<{ items: Video[] }>(
        `/api/videos?q=${encodeURIComponent(q)}&size=20`,
      ),
    enabled: !!q.trim(),
  });
  return (
    <DialogContent title="确认未纳管目录的影片归属">
      <div className="space-y-3 text-sm">
        <p className="break-all">
          {asset.root}/{asset.path}
        </p>
        <p>
          NFO 番号：{asset.metadata.code || "未知"}
          。仅关联号码一致的影片；不改写 STRM 或 NFO。
        </p>
        <Field label="搜索已入库影片">
          <Input value={q} onChange={(e) => setQ(e.target.value)} />
        </Field>
        {videos.error && <p className="text-err">{videos.error.message}</p>}
        {videos.data?.items.map((v) => (
          <div
            key={v.id}
            className="flex items-center justify-between gap-3 border-b border-line py-2"
          >
            <span>
              {v.code} {v.uncensored ? "-U" : "普通版"} · {v.title}
            </span>
            <Button
              size="sm"
              onClick={async () => {
                if (
                  !(await ask(
                    "确认资料关联",
                    `将该目录关联到 ${v.code}${v.uncensored ? " -U" : ""}，此后可在库维护中预览并清理目录内已识别的文件。`,
                  ))
                )
                  return;
                await run(
                  async () => {
                    await api.post(`/api/maintenance/assets/${asset.id}/bind`, {
                      video_id: v.id,
                    });
                    onDone();
                  },
                  {
                    success: "关联已保存",
                    invalidate: [["maintenance-assets"]],
                  },
                );
              }}
            >
              关联
            </Button>
          </div>
        ))}
      </div>
    </DialogContent>
  );
}

export function NfoLabel({
  meta,
}: {
  meta?: LocalMetadata & { conflict?: boolean };
}) {
  if (!meta) return null;
  return (
    <p className="mt-1 text-xs text-muted">
      {meta.conflict
        ? "本地 NFO 存在冲突，请在库维护中核对"
        : `本地 NFO：${meta.title || "无标题"} · ${meta.release_date || meta.year || "日期未知"}`}
    </p>
  );
}
