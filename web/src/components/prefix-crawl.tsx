import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Job, type Page, type PrefixRun } from "@/lib/api";
import { Pager, QueryNotice } from "@/components/common";
import { useLibraries, useMeta, useRun, useStatus } from "@/lib/queries";
import { navigate } from "@/lib/route";
import { ask } from "@/components/confirm";
import { Button } from "@/components/ui/button";
import { Field, Input, Check, Select, Textarea } from "@/components/ui/form";
import { Dialog, DialogContent } from "@/components/ui/overlay";
import { EmptyRow, Table, Td, Th } from "@/components/ui/data";

interface PrefixStat {prefix: string; total: number; covered: number; missing: number; removed: number; sample: string}
interface Statistics {scope_id: string; site: string; total: number; unrecognized: number; items: PrefixStat[]}

export function PrefixCrawl({filter, ids}: {filter: Record<string, unknown>; ids: number[]}) {
  const [open, setOpen] = useState(false);
  return <>
    <Button onClick={() => setOpen(true)}>统计前缀并批量抓取</Button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent title="按前缀批量抓取" className="max-h-[90vh] overflow-auto sm:max-w-4xl">
        {open && <PrefixForm filter={filter} ids={ids} onDone={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  </>;
}

function PrefixForm({filter, ids, onDone}: {filter: Record<string, unknown>; ids: number[]; onDone: () => void}) {
  const meta = useMeta();
  const {data: status} = useStatus();
  const {data: libraries = []} = useLibraries();
  const run = useRun();
  const enabled = Object.keys(meta.sites).filter(s => meta.site(s).prefix_search && status?.sites.some(entry => entry.name === s && entry.enabled));
  const [scope, setScope] = useState(ids.length ? "selected" : "filtered");
  const [site, setSite] = useState("missav");
  const [extraSites, setExtraSites] = useState<string[]>([]);
  const [stats, setStats] = useState<Statistics | null>(null);
  const [statsJob, setStatsJob] = useState<number | null>(null);
  const [statsError, setStatsError] = useState("");
  const statsTask = useQuery({queryKey:["prefix-stat-job",statsJob],queryFn:({signal})=>api.get<Job>(`/api/jobs/${statsJob}?summary=true`,signal),enabled:!!statsJob,refetchInterval:q=>q.state.data?.status==="done" || q.state.data?.status==="cancelled" ? false : 1000});
  const building = !!statsJob && !["done","cancelled"].includes(statsTask.data?.status ?? "");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("missing");
  const [manual, setManual] = useState("");
  const [mode, setMode] = useState("expand");
  const [library, setLibrary] = useState("1");
  const [maxPages, setMaxPages] = useState("0");
  const [strict, setStrict] = useState(true);
  const [detail, setDetail] = useState(true);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  useEffect(() => {
    if (enabled.length && !enabled.includes(site)) {setSite(enabled[0]); setStats(null); setExtraSites([]);}
  }, [enabled.join(","), site]);
  const resetScope = (value: string) => {setScope(value); setStats(null); setSelected(new Set());};
  useEffect(() => {
    const job = statsTask.data;
    if (!job || job.status!=="done" || job.state.partial_failure) return;
    let cancelled = false;
    api.post<Statistics>("/api/prefixes/stats",{site,scope_id:job.params.scope_id}).then(result=>{
      if (!cancelled) {setStats(result);setStatsJob(null);setPage(1);}
    }).catch(error=>{if (!cancelled) setStatsError(String(error));});
    return ()=>{cancelled=true;};
  }, [statsTask.data,site]);
  const calculate = async () => {
    setStatsError("");
    const result = await run(() => api.post<{job_id:number}>("/api/prefixes/stats", {
      background:true, site, scope_id: stats?.scope_id, query: scope === "filtered" ? filter : {}, ids: scope === "selected" ? ids : null,
    }), {invalidate: []});
    if (result) setStatsJob(result.job_id);
  };
  const changeSite = (value: string) => {
    setSite(value); setExtraSites([]);
    // 切换目标只更新源覆盖数，沿用同一影片快照。
    if (stats) void run(() => api.post<{job_id:number}>("/api/prefixes/stats", {background:true,site: value, scope_id: stats.scope_id}), {invalidate: []}).then(result => {if (result) setStatsJob(result.job_id);});
  };
  const manualPrefixes = manual.split(/[\s,，;；]+/).filter(Boolean).map(v => v.toUpperCase());
  const prefixes = [...new Set([...selected, ...manualPrefixes])];
  const rows = (stats?.items ?? []).filter(r => r.prefix.toLowerCase().includes(search.toLowerCase())).sort((a,b) => sort === "prefix" ? a.prefix.localeCompare(b.prefix) : (sort === "total" ? b.total - a.total : b.missing - a.missing) || a.prefix.localeCompare(b.prefix));
  const create = async () => {
    const result = await run(() => api.post<{job_id: number}>("/api/prefixes/jobs", {
      scope_id: stats?.scope_id, prefixes, sites: [site, ...extraSites], mode,
      library_id: mode === "expand" ? Number(library) : null, max_pages: Number(maxPages), strict, detail,
    }), {success: r => `已创建前缀抓取任务 #${r.job_id}`, invalidate: [["jobs"], ["status"]]});
    if (result) {onDone(); navigate("jobs", {job: String(result.job_id)});}
  };
  return <div className="space-y-4">
    <p className="text-sm text-muted">从影片范围统计番号前缀，到目标站点批量搜索。已有影片加源，新片按收录模式处理。</p>
    <div className="grid gap-3 sm:grid-cols-3">
      <Field label="统计范围"><Select aria-label="统计范围" value={scope} disabled={building || run.busy} onChange={e => resetScope(e.target.value)}>
        <option value="filtered">当前筛选的全部影片</option>
        <option value="selected" disabled={!ids.length}>勾选的 {ids.length} 部影片</option>
        <option value="all">整个影片库</option>
      </Select></Field>
      <Field label="目标源（覆盖统计）"><Select aria-label="目标源" value={site} onChange={e => changeSite(e.target.value)} disabled={run.busy || building}>
        {enabled.map(s => <option key={s} value={s}>{meta.label(s)}</option>)}
      </Select></Field>
      <div className="flex items-end"><Button disabled={run.busy || !!statsJob || !enabled.includes(site)} onClick={calculate}>{run.busy || building ? "统计中…" : stats ? "更新覆盖统计" : "统计前缀"}</Button></div>
    </div>
    {statsJob && <div className="space-y-2 rounded border border-line p-3">
      <QueryNotice query={statsTask} label="前缀统计进度" />
      <p className="text-sm">统计任务 #{statsJob}：{statsTask.data?.status==="cancelled" ? "已取消" : statsTask.data?.state.partial_failure ? "统计失败，可到任务页面重试" : statsTask.data?.status==="paused" ? "已暂停" : statsTask.data?.status==="done" ? "正在读取统计结果…" : `已处理 ${Number(statsTask.data?.state.processed ?? 0)} / ${Number(statsTask.data?.state.total ?? 0)} 部影片`}</p>
      {statsError && <p role="alert" className="text-sm text-err">{statsError}</p>}
      <div className="flex gap-2"><Button size="sm" onClick={()=>navigate("jobs",{job:String(statsJob)})}>查看统计任务</Button>
        {building && <Button size="sm" disabled={run.busy} onClick={()=>run(()=>api.post(`/api/jobs/${statsJob}/cancel`),{invalidate:[["prefix-stat-job",statsJob],["jobs"]]})}>取消统计</Button>}
        {!building && <Button size="sm" onClick={()=>{setStatsJob(null);setStatsError("");}}>重新统计</Button>}
      </div>
    </div>}
    {!enabled.length && <p className="text-sm text-warn">请先在设置中启用支持关键词搜索的站点。</p>}
    {stats && <>
      <p className="text-sm">快照范围 {stats.total} 部，识别 {stats.items.length} 个前缀；未识别 {stats.unrecognized} 部。覆盖统计：{meta.label(stats.site)}。手动删源不会自动恢复。</p>
      <div className="flex flex-wrap gap-2">
        <Input aria-label="搜索前缀" placeholder="搜索前缀" value={search} onChange={e => {setSearch(e.target.value);setPage(1);}} className="w-36" />
        <Select aria-label="前缀排序" value={sort} onChange={e => {setSort(e.target.value);setPage(1);}}><option value="missing">缺源多的优先</option><option value="total">影片多的优先</option><option value="prefix">按前缀名称</option></Select>
        <Button size="sm" onClick={() => setSelected(new Set(rows.map(r => r.prefix)))}>全选匹配的 {rows.length} 个前缀</Button>
        <Button size="sm" onClick={() => setSelected(new Set())}>清空选择</Button>
        <Button size="sm" disabled={!prefixes.length} onClick={async () => {try {await navigator.clipboard.writeText(prefixes.join("\n")); setCopied(true); setCopyError("");} catch {setCopyError("复制失败，可从手动前缀框选中复制"); setManual(prefixes.join("\n"));}}}>{copied ? "已复制" : "复制已选前缀"}</Button>
      </div>
      {copyError && <p className="text-sm text-warn">{copyError}</p>}
      <div className="max-h-72 overflow-auto"><Table className="min-w-[650px]"><thead><tr><Th>选择</Th><Th>前缀</Th><Th>影片数</Th><Th>已有目标源</Th><Th>缺目标源</Th><Th>手动删源</Th><Th>示例</Th></tr></thead><tbody>
        {rows.slice((page-1)*50,page*50).map(row => <tr key={row.prefix}><Td><input type="checkbox" aria-label={`选择前缀 ${row.prefix}`} checked={selected.has(row.prefix)} onChange={e => setSelected(previous => {const next = new Set(previous); e.target.checked ? next.add(row.prefix) : next.delete(row.prefix); return next;})} /></Td><Td>{row.prefix}</Td><Td>{row.total}</Td><Td>{row.covered}</Td><Td>{row.missing}</Td><Td>{row.removed}</Td><Td>{row.sample}</Td></tr>)}
        {!rows.length && <EmptyRow cols={7}>没有匹配的前缀，可以手动填写。</EmptyRow>}
      </tbody></Table></div>
      <Pager page={page} size={50} total={rows.length} onPage={setPage} />
    </>}
    <Field label="手动补充前缀" hint="每行一个，也可以用空格或逗号分隔；与勾选结果合并去重。"><Textarea aria-label="手动补充前缀" value={manual} onChange={e => setManual(e.target.value)} placeholder={"JUFE\nMDBK"} /></Field>
    <div className="flex flex-wrap gap-3"><span className="text-sm text-muted">同时抓取其他目标源：</span>{enabled.filter(s => s !== site).map(s => <Check key={s} checked={extraSites.includes(s)} onChange={v => setExtraSites(prev => v ? [...prev,s] : prev.filter(n => n !== s))}>{meta.label(s)}</Check>)}</div>
    <div className="grid gap-3 sm:grid-cols-3">
      <Field label="收录模式"><Select aria-label="收录模式" value={mode} onChange={e => setMode(e.target.value)}><option value="expand">补源并扩库</option><option value="existing">仅给快照内影片补源</option></Select></Field>
      {mode === "expand" && <Field label="新片输出库"><Select aria-label="新片输出库" value={library} onChange={e => setLibrary(e.target.value)}>{libraries.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}</Select></Field>}
      <Field label="每个前缀最多页数" hint="0 表示完整扫描；不因遇到已有影片提前停止。"><Input aria-label="每个前缀最多页数" type="number" min={0} max={10000} value={maxPages} onChange={e => setMaxPages(e.target.value)} /></Field>
    </div>
    <div className="flex flex-wrap gap-4"><Check checked={strict} onChange={setStrict}>仅收录番号前缀符合的结果</Check><Check checked={detail} onChange={setDetail}>抓取新源详情</Check></div>
    {!strict && <p className="text-sm text-warn">关闭前缀核验后，标题等字段命中的其他系列也会按所选模式处理。</p>}
    {prefixes.some(p => ["FC2", "FC2PPV", "FC2-PPV"].includes(p)) && <p className="text-sm text-warn">FC2 搜索结果可能很多，建议设置页数上限。</p>}
    <p className="text-sm text-muted">已有影片保留输出库归属；新片遵守排除规则。页面可以关闭，任务在后台继续。</p>
    <Button variant="primary" disabled={run.busy || !!statsJob || !stats || !prefixes.length || !enabled.includes(site) || !Number.isInteger(Number(maxPages)) || Number(maxPages) < 0 || Number(maxPages) > 10000} onClick={create}>创建批次：{prefixes.length} 个前缀 × {extraSites.length + 1} 个源</Button>
  </div>;
}

const scanLabels = {scanning: "扫描中", complete: "完整扫描", limited: "达到页数上限", failed: "部分失败", cancelled: "已取消"};

export function PrefixProgress({job}: {job: Job}) {
  const meta = useMeta();
  const run = useRun();
  const [q, setQ] = useState("");
  const [search,setSearch] = useState("");
  const [page,setPage] = useState(1);
  useEffect(()=>{const timer=setTimeout(()=>{setSearch(q);setPage(1);},250);return ()=>clearTimeout(timer);},[q]);
  const active = job.status==="running" || job.status==="paused";
  const progressQuery = useQuery({queryKey:["prefix-progress",job.id,search,page,job.status],queryFn:({signal})=>api.get<Page<PrefixRun>>(`/api/prefixes/jobs/${job.id}/progress?q=${encodeURIComponent(search)}&limit=50&offset=${(page-1)*50}`,signal),refetchInterval:active ? 3000 : false,placeholderData:keepPreviousData});
  const summary = useQuery({queryKey:["prefix-summary",job.id,job.status],queryFn:({signal})=>api.get<{seen:number;new:number;sources_added:number;skipped:number}>(`/api/prefixes/jobs/${job.id}/summary`,signal),refetchInterval:active ? 5000 : false});
  const progress = progressQuery.data;
  return <div className="space-y-2">
    <h4 className="font-semibold">前缀抓取进度</h4>
    <QueryNotice query={summary} label="前缀汇总" />
    {summary.data && <p className="text-sm">匹配影片 {summary.data.seen} 部 · 新影片 {summary.data.new} 部 · 新增源 {summary.data.sources_added} 个 · 跳过 {summary.data.skipped} 条</p>}
    <Input aria-label="筛选前缀任务" placeholder="筛选前缀任务" value={q} onChange={e => setQ(e.target.value)} />
    <QueryNotice query={progressQuery} label="前缀进度" />
    <div className="max-h-80 overflow-auto"><Table className="min-w-[900px] [&_td]:whitespace-nowrap"><thead><tr><Th>目标源 / 前缀</Th><Th>扫描状态</Th><Th>已抓页数</Th><Th>影片 / 新片</Th><Th>新增源</Th><Th>不符 / 范围外</Th><Th>删源 / 排除</Th><Th>后续任务</Th><Th>操作</Th></tr></thead><tbody>
      {progress?.items.map(row => <tr key={`${row.site}/${row.prefix}`}>
        <Td>{meta.label(row.site)} / {row.prefix}</Td><Td>{scanLabels[row.scan_status]}</Td><Td>{row.pages_done ?? 0} / {row.last_page ?? "?"}</Td>
        <Td>{row.seen ?? 0} / {row.new ?? 0}</Td><Td>{row.sources_added ?? 0}</Td><Td>{row.mismatch ?? 0} / {row.outside ?? 0}</Td><Td>{row.removed ?? 0} / {row.excluded ?? 0}</Td>
        <Td>待处理 {row.pending ?? 0}，执行 {row.running ?? 0}，失败 {row.failed ?? 0}</Td>
        <Td>{!row.cancelled && ["running","paused"].includes(job.status) && <Button size="sm" disabled={run.busy} onClick={async () => {
          if (await ask(`取消 ${meta.label(row.site)} / ${row.prefix}？`, "停止该前缀后续列表和详情任务，保留已抓取结果。正在处理的一部影片或详情会完成。", {confirmText: "取消此前缀", danger: true}))
            await run(() => api.post(`/api/prefixes/jobs/${job.id}/cancel`, {site: row.site, prefix: row.prefix}), {success: "此前缀已取消", invalidate: [["jobs"], ["status"], ["tasks",job.id],["prefix-progress",job.id],["prefix-summary",job.id]]});
        }}>取消</Button>}</Td>
      </tr>)}
      {progress && !progress.items.length && <EmptyRow cols={9}>没有符合筛选的前缀</EmptyRow>}
    </tbody></Table></div>
    <Pager page={page} size={50} total={progress?.total ?? 0} onPage={setPage} />
    <p className="text-xs text-muted">批次汇总去重；完整扫描表示列表处理完成，详情失败仍单独记录，新增源未必已通过播放验证。</p>
  </div>;
}
