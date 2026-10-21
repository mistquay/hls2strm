import { useQuery } from "@tanstack/react-query";
import { Archive } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/data";
import { Select } from "@/components/ui/form";
import { OperationPreview } from "./operation-preview";
import { QueryNotice } from "./common";
import { Dialog, DialogContent } from "@/components/ui/overlay";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useMeta, useRun } from "@/lib/queries";
import { navigate } from "@/lib/route";

interface Retirement { site: string; job_id: number; created_at: number; status: string | null; tasks: Record<string, number> }

const INVALIDATE = [["site-retirements"], ["jobs"], ["libraries"], ["subscriptions"], ["settings"], ["status"]];
const STATUS: Record<string, string> = { running: "进行中", paused: "已暂停", done: "已完成", cancelled: "已取消" };

export function SiteRetirement() {
  const run = useRun();
  const meta = useMeta();
  const [open, setOpen] = useState(false);
  const [site, setSite] = useState("");
  const [restoring, setRestoring] = useState("");
  const [busy, setBusy] = useState(false);
  const records = useQuery({
    queryKey: ["site-retirements"],
    queryFn: ({ signal }) => api.get<Retirement[]>("/api/site-retirements", signal),
    enabled: open, refetchInterval: open ? 5000 : false,
  });
  const retired = new Set(records.data?.map(r => r.site));
  const choices = Object.entries(meta.sites).filter(([key]) => !retired.has(key));
  const current = site && !retired.has(site) ? site : choices[0]?.[0] ?? "";

  const busyRun = async <T,>(fn: () => Promise<T>, opts: Parameters<typeof run<T>>[1]) => {
    setBusy(true);
    try { return await run(fn, opts); } finally { setBusy(false); }
  };
  const toJob = (id: number) => { setOpen(false); navigate("jobs", { job: String(id) }); };
  const restore = async (name: string) => {
    const r = await busyRun(() => api.post<{ job_id: number }>(`/api/sites/${name}/retirement/restore`),
      { success: r => `已开始恢复，任务 #${r.job_id}`, invalidate: INVALIDATE });
    setRestoring("");
    if (r) toJob(r.job_id);
  };

  return <>
    <Button size="sm" onClick={() => setOpen(true)}><Archive />站点清退</Button>
    <Dialog open={open} onOpenChange={v => !busy && setOpen(v)}>
      <DialogContent className="max-w-3xl" title="站点清退" footer={<>
        <Button disabled={busy} onClick={() => setOpen(false)}>关闭</Button>
      </>}>
        <div className="space-y-4 text-sm">
          <QueryNotice query={records} label="清退记录" />
          <p className="text-muted">
            彻底不用一个站点：停用站点和它的订阅，删掉所有影片在这个站的源。别的站还有能用的源的影片留在库里；没有的下架，
            它在各输出库里的文件原样搬进归档目录，按库分开，已刮削的整个影片目录放「已刮削」、收件目录里没刮削的 strm 放「未刮削」，媒体库看不到、刮削器也监控不到。
            恢复时原样搬回。只看本地记录，不抓源站、不探测播放。
          </p>
          <Button size="sm" disabled={busy} onClick={() => { setOpen(false); navigate("maintenance", { tab: "unavailable" }); }}>
            核查无可用源影片及清退残留
          </Button>
          {!!records.data?.length && <div className="space-y-2 rounded border border-line p-3">
            <div className="font-medium">已清退</div>
            {records.data.map(r => <div key={r.site} className="flex flex-wrap items-center gap-2">
              <span>{meta.label(r.site)}</span>
              <span className="text-muted">{fmtTime(r.created_at)} · 任务 #{r.job_id} {STATUS[r.status ?? ""] ?? ""}
                {r.tasks.failed ? ` · ${r.tasks.failed} 部失败` : ""}</span>
              <Button size="sm" onClick={() => toJob(r.job_id)}>查看任务</Button>
              <Button size="sm" disabled={busy || ["running", "paused"].includes(r.status ?? "")}
                onClick={() => setRestoring(r.site)}>恢复</Button>
            </div>)}
          </div>}
          {restoring && <Notice actions={<>
            <Button size="sm" disabled={busy} onClick={() => setRestoring("")}>取消</Button>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => restore(restoring)}>确认恢复</Button>
          </>}>
            恢复 {meta.label(restoring)}：重新启用站点和清退时停用的订阅，恢复这些影片在这个站的源，下架的从归档目录搬回原处。
            原处已经有文件的不覆盖，留在归档目录里。
          </Notice>}
          <div className="flex gap-2">
            <Select value={current} disabled={busy} onChange={e => setSite(e.target.value)}>
              {choices.map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
            </Select>
          </div>
          {!!current && <OperationPreview request={{kind:"retirement",payload:{site:current}}} disabled={busy || !!records.error || records.isPending} confirmText="停用站点并清退" onApplied={toJob} />}
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
