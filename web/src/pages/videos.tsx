import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownWideNarrow,
  ArrowUpNarrowWide,
  FilmIcon,
  FolderMinus,
  FolderPlus,
  LayoutGrid,
  List,
  MoreHorizontal,
  PanelRight,
  Play,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Pager, streamState } from "@/components/common";
import { FacetSelect, facetName, rememberFacet, useFacetNames } from "@/components/facet-select";
import { playVideo } from "@/components/player";
import { Button } from "@/components/ui/button";
import { Chip, Code, EmptyRow, Panel, Table, Td, Th } from "@/components/ui/data";
import { Field, Input, Segmented, Select } from "@/components/ui/form";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "@/components/ui/overlay";
import { openDetail } from "@/components/video-detail";
import { OperationPreview, type OperationRequest } from "@/components/operation-preview";
import { Dialog, DialogContent } from "@/components/ui/overlay";
import { selectionFilter } from "@/lib/video-selection";
import { api, type FacetField, type Page, type Video, qs } from "@/lib/api";
import { fmtClockDur } from "@/lib/format";
import { subtitleTag } from "@/lib/labels";
import { useLibraries, useMeta, useRun } from "@/lib/queries";
import { replaceParams, useRoute } from "@/lib/route";
import { cn } from "@/lib/utils";

// 地址栏参数：列表类可以重复（has_site=jable&has_site=missav）
const LIST_KEYS = ["has_site", "lacks_site", "model", "category", "tag", "maker", "quality"] as const;
type ListKey = (typeof LIST_KEYS)[number];
const FACETS: { key: ListKey; field: FacetField; label: string }[] = [
  { key: "model", field: "models", label: "女优" },
  { key: "category", field: "categories", label: "分类" },
  { key: "tag", field: "tags", label: "标签" },
  { key: "maker", field: "makers", label: "发行商" },
  { key: "quality", field: "quality", label: "画质" },
];
const ADVANCED = ["has_site", "lacks_site", "sources", "subtitle", "uncensored", ...FACETS.map(f => f.key), "release_from", "release_to", "added", "dur_min", "dur_max"];

const SORTS: Record<string, string> = {
  created: "入库时间",
  release: "上市日期",
  duration: "时长",
  code: "番号",
  views: "观看数",
  updated: "更新时间",
};
const STATUS: Record<string, string> = { "": "全部影片", active: "可用的", no_detail: "缺详情", no_output: "没输出", pending: "待归入", gone: "已下架", removed: "已删源" };
const SOURCES: Record<string, string> = { "": "不限", single: "只有一个", multi: "多个", failing: "有失败的", none: "没有可用的" };
const SUBTITLE: Record<string, string> = { "": "不限", zh: "中字", en: "英字", none: "无字幕" };
// 用数组：对象里 "1" 这类数字键会排到 "" 前面
const ADDED_OPTIONS = [
  { value: "", label: "不限" },
  { value: "1", label: "24 小时内" },
  { value: "3", label: "3 天内" },
  { value: "7", label: "7 天内" },
  { value: "30", label: "30 天内" },
];
const ADDED: Record<string, string> = Object.fromEntries(ADDED_OPTIONS.map(o => [o.value, o.label]));

export default function Videos() {
  const { params } = useRoute();
  const [dedupe,setDedupe] = useState<OperationRequest|null>(null);
  const meta = useMeta();
  const { data: libraries = [] } = useLibraries();
  const p = (k: string) => params.get(k) ?? "";
  const list = (k: ListKey) => params.getAll(k);
  const page = Number(p("page")) || 1;
  const size = Number(p("size")) || 50;
  const view = p("view") === "grid" ? "grid" : "table";
  const sort = SORTS[p("sort")] ? p("sort") : "created";
  const order = p("order") === "asc" ? "asc" : "desc";

  /** 改筛选条件：除了翻页，都回到第 1 页。 */
  const update = (patch: Record<string, string | string[] | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      next.delete(k);
      if (Array.isArray(v)) v.forEach(x => next.append(k, x));
      else if (v) next.set(k, v);
    }
    if (!("page" in patch)) next.delete("page");
    replaceParams(next);
  };

  // 搜索框：输入停 400ms 再查
  const [q, setQ] = useState(p("q"));
  useEffect(() => setQ(params.get("q") ?? ""), [params]);
  useEffect(() => {
    if (q === p("q")) return;
    const t = setTimeout(() => update({ q }), 400);
    return () => clearTimeout(t);
  }, [q]);

  const apiQuery = useMemo(() => {
    const days = Number(params.get("added"));
    return qs({
      q: params.get("q"),
      filter: params.get("filter"),
      library_id: params.get("library"),
      sources: params.get("sources"),
      subtitle: params.get("subtitle"),
      uncensored: params.get("uncensored") === "1" ? true : params.get("uncensored") === "0" ? false : null,
      release_from: params.get("release_from"),
      release_to: params.get("release_to"),
      added_from: days ? Math.floor(Date.now() / 1000 / 3600) * 3600 - days * 86400 : null,
      duration_min: Number(params.get("dur_min")) ? Number(params.get("dur_min")) * 60 : null,
      duration_max: Number(params.get("dur_max")) ? Number(params.get("dur_max")) * 60 : null,
      ...Object.fromEntries(LIST_KEYS.map(k => [k, params.getAll(k)])),
      sort,
      order,
      page,
      size,
    });
  }, [params, sort, order, page, size]);

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ["videos", apiQuery],
    queryFn: ({ signal }) => api.get<Page<Video>>(`/api/videos${apiQuery}`, signal),
    placeholderData: keepPreviousData,
  });
  const items = data?.items ?? [];

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [allMatching,setAllMatching] = useState(false);
  const selectionQuery = JSON.stringify(selectionFilter(apiQuery));
  useEffect(() => {setSelected(new Set());setAllMatching(false);}, [selectionQuery]);
  const toggle = (id: number) =>
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allOnPage = items.length > 0 && items.every(v => selected.has(v.id));

  const advancedCount = ADVANCED.filter(k => params.has(k)).length;
  const [showFilters, setShowFilters] = useState(advancedCount > 0);
  // 地址栏里的女优、分类是 id，取常见候选来显示名字（FACETS 是常量，循环里调 hook 的次数固定）
  FACETS.forEach(f => useFacetNames(f.field, list(f.key).length > 0));

  const addFacet = (key: ListKey, field: FacetField, item: string, name: string) => {
    rememberFacet(field, item, name);
    if (!list(key).includes(item)) update({ [key]: [...list(key), item] });
  };

  return (
    <>
      <Dialog open={!!dedupe} onOpenChange={open=>!open && setDedupe(null)}>
        <DialogContent title="同片整理" className="max-h-[90vh] overflow-auto sm:max-w-4xl">
          {dedupe && <OperationPreview request={dedupe} onRequestChange={setDedupe} onApplied={()=>setDedupe(null)} confirmText="确认整理并清理重复文件" />}
        </DialogContent>
      </Dialog>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={()=>setDedupe({kind:"dedupe",payload:{}})}>同片整理</Button>
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input value={q} onChange={e => setQ(e.target.value)} placeholder="搜番号、标题、女优、标签" className="pl-8" aria-label="搜索影片" />
        </div>
        <Select value={p("filter")} onChange={e => update({ filter: e.target.value })} aria-label="影片状态" className="w-28">
          {Object.entries(STATUS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </Select>
        <Select value={p("library")} onChange={e => update({ library: e.target.value })} aria-label="输出库" className="w-36">
          <option value="">全部输出库</option>
          {libraries.map(l => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </Select>
        <div className="flex items-center">
          <Select value={sort} onChange={e => update({ sort: e.target.value })} aria-label="排序" className="w-28 [&_select]:rounded-r-none">
            {Object.entries(SORTS).map(([k, v]) => (
              <option key={k} value={k}>
                按{v}
              </option>
            ))}
          </Select>
          <Button
            size="icon"
            className="-ml-px rounded-l-none"
            onClick={() => update({ order: order === "desc" ? "asc" : null })}
            title={order === "desc" ? "从大到小（新的在前）" : "从小到大（旧的在前）"}
            aria-label="切换排序方向"
          >
            {order === "desc" ? <ArrowDownWideNarrow /> : <ArrowUpNarrowWide />}
          </Button>
        </div>
        <Button className={cn(showFilters && "bg-panel-2")} onClick={() => setShowFilters(v => !v)} aria-expanded={showFilters}>
          <SlidersHorizontal />
          筛选
          {advancedCount > 0 && <span className="rounded bg-accent px-1.5 text-xs text-accent-ink">{advancedCount}</span>}
        </Button>
        <Segmented
          value={view}
          onChange={v => update({ view: v === "grid" ? "grid" : null, page: p("page") || null })}
          options={[
            { value: "table", label: <List />, title: "表格" },
            { value: "grid", label: <LayoutGrid />, title: "封面墙" },
          ]}
        />
      </div>

      {showFilters && <Filters params={params} update={update} list={list} />}

      <ActiveFilters params={params} update={update} libraries={libraries} siteLabel={meta.label} />

      <Button size="sm" disabled={isFetching || isError || !data?.total} onClick={()=>setAllMatching(true)}>按当前筛选的全部 {data?.total ?? 0} 部批量处理</Button>
      {(selected.size > 0 || allMatching) && <BatchBar ids={[...selected]} filter={selectionFilter(apiQuery)} allMatching={allMatching} total={data?.total ?? 0} onDone={() => {setSelected(new Set());setAllMatching(false);}} />}

      {isError && <p className="text-sm text-err">加载失败：{(error as Error).message}</p>}

      {view === "table" ? (
        <Panel bodyClassName="px-4 py-0" className={cn(isFetching && "opacity-80")}>
          <Table>
            <thead>
              <tr>
                <Th className="w-8">
                  <input
                    type="checkbox"
                    aria-label="选中本页"
                    checked={allOnPage}
                    onChange={() => setSelected(prev=>{const next=new Set(prev);items.forEach(v=>allOnPage ? next.delete(v.id) : next.add(v.id));return next;})}
                  />
                </Th>
                <Th className="w-[104px]" />
                <Th>番号</Th>
                <Th>标题</Th>
                <Th className="text-right">时长</Th>
                <Th>上市</Th>
                <Th>源</Th>
                <Th>所在库</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {items.map(v => (
                <VideoRow key={v.id} v={v} selected={selected.has(v.id)} onToggle={() => toggle(v.id)} addFacet={addFacet} />
              ))}
              {items.length === 0 && !isFetching && <EmptyRow cols={9}>没有符合条件的影片。{params.toString() ? "放宽一些筛选条件试试。" : ""}</EmptyRow>}
            </tbody>
          </Table>
        </Panel>
      ) : (
        <div className={cn("grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6", isFetching && "opacity-80")}>
          {items.map(v => (
            <VideoCard key={v.id} v={v} selected={selected.has(v.id)} onToggle={() => toggle(v.id)} />
          ))}
          {items.length === 0 && !isFetching && <p className="col-span-full py-10 text-center text-sm text-muted">没有符合条件的影片。</p>}
        </div>
      )}

      <Pager
        page={page}
        size={size}
        total={data?.total ?? 0}
        onPage={n => update({ page: n > 1 ? String(n) : null })}
        onSize={n => update({ size: n === 50 ? null : String(n) })}
      />
    </>
  );
}

function Filters({
  params,
  update,
  list,
}: {
  params: URLSearchParams;
  update: (patch: Record<string, string | string[] | null>) => void;
  list: (k: ListKey) => string[];
}) {
  const meta = useMeta();
  const sites = Object.entries(meta.sites);
  const p = (k: string) => params.get(k) ?? "";
  const toggleSite = (key: "has_site" | "lacks_site", site: string) =>
    update({ [key]: list(key).includes(site) ? list(key).filter(x => x !== site) : [...list(key), site] });

  return (
    <Panel bodyClassName="grid gap-x-6 gap-y-4 md:grid-cols-2 xl:grid-cols-3">
      <Field label="有这些站的源（任一）">
        <div className="flex flex-wrap gap-1.5">
          {sites.map(([k, m]) => (
            <SiteToggle key={k} on={list("has_site").includes(k)} onClick={() => toggleSite("has_site", k)}>
              {m.label}
            </SiteToggle>
          ))}
        </div>
      </Field>
      <Field label="缺这些站的源（都没有）">
        <div className="flex flex-wrap gap-1.5">
          {sites.map(([k, m]) => (
            <SiteToggle key={k} on={list("lacks_site").includes(k)} onClick={() => toggleSite("lacks_site", k)}>
              {m.label}
            </SiteToggle>
          ))}
        </div>
      </Field>
      <Field label="可用的源">
        <Segmented size="sm" value={p("sources")} onChange={v => update({ sources: v })} options={Object.entries(SOURCES).map(([value, label]) => ({ value, label }))} className="flex-wrap self-start" />
      </Field>
      <Field label="字幕（按可用的源）">
        <Segmented size="sm" value={p("subtitle")} onChange={v => update({ subtitle: v })} options={Object.entries(SUBTITLE).map(([value, label]) => ({ value, label }))} className="self-start" />
      </Field>
      <Field label="无码流出版">
        <Segmented
          size="sm"
          value={p("uncensored")}
          onChange={v => update({ uncensored: v })}
          className="self-start"
          options={[
            { value: "", label: "不限" },
            { value: "1", label: "只看无码流出" },
            { value: "0", label: "排除" },
          ]}
        />
      </Field>
      <Field label="入库时间">
        <Segmented size="sm" value={p("added")} onChange={v => update({ added: v })} options={ADDED_OPTIONS} className="flex-wrap self-start" />
      </Field>
      {FACETS.map(f => (
        <Field key={f.key} label={f.label}>
          <FacetSelect field={f.field} label={f.label} value={list(f.key)} onChange={v => update({ [f.key]: v })} />
        </Field>
      ))}
      <Field label="上市日期">
        <div className="flex items-center gap-1.5">
          <Input type="date" value={p("release_from")} onChange={e => update({ release_from: e.target.value })} aria-label="上市日期从" />
          <span className="text-muted">至</span>
          <Input type="date" value={p("release_to")} onChange={e => update({ release_to: e.target.value })} aria-label="上市日期到" />
        </div>
      </Field>
      <Field label="时长（分钟）">
        <div className="flex items-center gap-1.5">
          <Input type="number" min={0} placeholder="最短" value={p("dur_min")} onChange={e => update({ dur_min: e.target.value })} aria-label="最短分钟" />
          <span className="text-muted">至</span>
          <Input type="number" min={0} placeholder="最长" value={p("dur_max")} onChange={e => update({ dur_max: e.target.value })} aria-label="最长分钟" />
        </div>
      </Field>
    </Panel>
  );
}

function SiteToggle({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={cn(
        "h-7 rounded-md border px-2.5 text-[13px] transition-colors",
        on ? "border-accent bg-accent-soft text-accent" : "border-line bg-panel text-muted hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

/** 当前生效的筛选条件，一个一个能删。 */
function ActiveFilters({
  params,
  update,
  libraries,
  siteLabel,
}: {
  params: URLSearchParams;
  update: (patch: Record<string, string | string[] | null>) => void;
  libraries: { id: number; name: string }[];
  siteLabel: (n: string) => string;
}) {
  const chips: { key: string; label: string; remove: () => void }[] = [];
  const single = (k: string, label: (v: string) => string) => {
    const v = params.get(k);
    if (v) chips.push({ key: k, label: label(v), remove: () => update({ [k]: null }) });
  };
  const multi = (k: ListKey, label: (v: string) => string) =>
    params.getAll(k).forEach(v => chips.push({ key: k + v, label: label(v), remove: () => update({ [k]: params.getAll(k).filter(x => x !== v) }) }));

  single("q", v => `搜索：${v}`);
  single("filter", v => STATUS[v] ?? v);
  single("library", v => `库：${libraries.find(l => String(l.id) === v)?.name ?? "#" + v}`);
  multi("has_site", v => `有 ${siteLabel(v)} 源`);
  multi("lacks_site", v => `缺 ${siteLabel(v)} 源`);
  single("sources", v => `可用的源：${SOURCES[v] ?? v}`);
  single("subtitle", v => `字幕：${SUBTITLE[v] ?? v}`);
  single("uncensored", v => (v === "1" ? "无码流出" : "排除无码流出"));
  FACETS.forEach(f => multi(f.key, v => `${f.label}：${facetName(f.field, v)}`));
  if (params.get("release_from") || params.get("release_to"))
    chips.push({
      key: "release",
      label: `上市：${params.get("release_from") || "…"} 至 ${params.get("release_to") || "…"}`,
      remove: () => update({ release_from: null, release_to: null }),
    });
  single("added", v => `入库：${ADDED[v] ?? v}`);
  if (params.get("dur_min") || params.get("dur_max"))
    chips.push({
      key: "dur",
      label: `时长：${params.get("dur_min") || 0}–${params.get("dur_max") || "∞"} 分钟`,
      remove: () => update({ dur_min: null, dur_max: null }),
    });
  if (!chips.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {chips.map(c => (
        <span key={c.key} className="inline-flex h-7 items-center gap-1 rounded-md border border-accent/30 bg-accent-soft pl-2.5 pr-1 text-[13px] text-accent">
          {c.label}
          <button type="button" onClick={c.remove} className="rounded p-0.5 hover:bg-accent/15" aria-label={`去掉条件 ${c.label}`}>
            <X className="size-3.5" />
          </button>
        </span>
      ))}
      <Button
        size="sm"
        variant="quiet"
        onClick={() => {
          const keep = new URLSearchParams();
          for (const k of ["sort", "order", "view", "size"]) if (params.get(k)) keep.set(k, params.get(k)!);
          replaceParams(keep);
        }}
      >
        清空全部条件
      </Button>
    </div>
  );
}

function BatchBar({ ids, onDone,filter,allMatching,total }: { ids: number[]; onDone: () => void;filter:Record<string,unknown>;allMatching:boolean;total:number }) {
  const qc = useQueryClient();
  const { data: libraries = [] } = useLibraries();
  const meta = useMeta();
  const [operation,setOperation] = useState<OperationRequest|null>(null);
  const batch = (body: Record<string, unknown>) => {
    setOperation({kind:"selection",payload:{...body,ids:allMatching ? null : ids,query:allMatching ? filter : {}}});
  };
  return (
    <div className="sticky top-14 z-20 flex flex-wrap items-center gap-2 rounded-lg border border-accent/40 bg-panel px-3 py-2 shadow-[0_8px_24px_-12px_rgb(0_0_0/0.3)]">
      <span className="text-sm">
        {allMatching ? "当前筛选全部" : "跨页已选"} <b>{allMatching ? total : ids.length}</b> 部
      </span>
      <div className="mx-1 h-4 w-px bg-line" />
      <Menu>
        <MenuTrigger asChild>
          <Button size="sm">
            <Search />
            查找其他源
          </Button>
        </MenuTrigger>
        <MenuContent align="start">
          <MenuItem onSelect={() => batch({ action: "probe" })}>所有还没有源的站点</MenuItem>
          <MenuSeparator />
          <MenuLabel>只到这个站找</MenuLabel>
          {Object.entries(meta.sites).map(([site, m]) => (
            <MenuItem key={site} onSelect={() => batch({ action: "probe", sites: [site] })}>
              {m.label}
            </MenuItem>
          ))}
        </MenuContent>
      </Menu>
      <Button size="sm" onClick={() => batch({ action: "refresh" })}>
        <RefreshCw />
        刷新详情
      </Button>
      <Menu>
        <MenuTrigger asChild>
            <Button size="sm">
            <FolderPlus />
            加入输出库
          </Button>
        </MenuTrigger>
        <MenuContent align="start">
          {libraries.map(l => (
            <MenuItem key={l.id} onSelect={() => batch({ action: "add", library_id: l.id })}>
              {l.name}
            </MenuItem>
          ))}
        </MenuContent>
      </Menu>
      <Menu>
        <MenuTrigger asChild>
            <Button size="sm" disabled={allMatching} title={allMatching ? "移出输出库请显式选择影片" : undefined}>
              <FolderMinus />
            移出输出库
          </Button>
        </MenuTrigger>
        <MenuContent align="start">
          {libraries.map(l => (
            <MenuItem
              key={l.id}
              onSelect={() => batch({ action: "remove", library_id: l.id })}
            >
              {l.name}
            </MenuItem>
          ))}
        </MenuContent>
      </Menu>
      <Menu>
        <MenuTrigger asChild>
            <Button size="sm" disabled={allMatching} title={allMatching ? "删除源请显式选择影片" : undefined}>
              <Trash2 />
            删除源
          </Button>
        </MenuTrigger>
        <MenuContent align="start">
          <MenuLabel>删掉选中影片在哪个站的源</MenuLabel>
          {Object.entries(meta.sites).map(([site, m]) => (
            <MenuItem
              key={site}
              danger
              onSelect={() => batch({ action: "remove_source", sites: [site] })}
            >
              {m.label}
            </MenuItem>
          ))}
          <MenuSeparator />
          <MenuItem onSelect={() => batch({ action: "restore_source" })}>恢复删掉的源</MenuItem>
        </MenuContent>
      </Menu>
      <Button size="sm" variant="quiet" className="ml-auto" onClick={onDone}>
        取消选择
      </Button>
      <Dialog open={!!operation} onOpenChange={open=>!open && setOperation(null)}>
        <DialogContent title="确认批量处理范围" className="max-w-3xl" footer={<Button onClick={()=>setOperation(null)}>关闭</Button>}>
          {operation?.payload.action==="remove" && <p className="text-sm text-warn">移出会删除本服务输出；外部整理库的独占目录移进回收区。规则或来源库以后可能把符合条件的影片重新加入。</p>}
          {operation?.payload.action==="remove_source" && <p className="text-sm text-warn">删除后不会自动重新使用该站源。没有其他可用源的影片会移出输出库，文件归档；可通过“恢复删掉的源”恢复。</p>}
          {operation && <OperationPreview request={operation} onApplied={()=>{setOperation(null);[["videos"],["jobs"],["libraries"],["status"]].forEach(queryKey=>qc.invalidateQueries({queryKey}));onDone();}} />}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Thumb({ v, className }: { v: Video; className?: string }) {
  const [broken, setBroken] = useState(false);
  const src = v.thumb_url || v.cover_url;
  if (!src || broken)
    return (
      <div className={cn("flex items-center justify-center bg-panel-2 text-muted", className)}>
        <FilmIcon className="size-5 opacity-50" />
      </div>
    );
  return <img loading="lazy" referrerPolicy="no-referrer" src={src} alt="" onError={() => setBroken(true)} className={cn("bg-panel-2 object-cover", className)} />;
}

function SourceChips({ v }: { v: Video }) {
  return (
    <div className="flex flex-wrap gap-1">
      {v.sources.map(src => {
        const st = streamState(src);
        const tone = src.status !== "active" ? "err" : src.fail_streak > 0 ? "warn" : "neutral";
        const sub = subtitleTag(src.subtitle);
        return (
          <Chip key={src.id} tone={tone} title={`${src.label} ${src.key}：${st.text}${src.last_error ? "\n" + src.last_error : ""}`}>
            {src.label}
            {sub && <span className="opacity-70">·{sub}</span>}
          </Chip>
        );
      })}
      {v.sources.length === 0 && <span className="text-xs text-err">没有源</span>}
    </div>
  );
}

function VideoRow({
  v,
  selected,
  onToggle,
  addFacet,
}: {
  v: Video;
  selected: boolean;
  onToggle: () => void;
  addFacet: (key: ListKey, field: FacetField, item: string, name: string) => void;
}) {
  const run = useRun();
  const qc = useQueryClient();
  return (
    <tr className={cn("group", selected && "bg-accent-soft/40")}>
      <Td>
        <input type="checkbox" checked={selected} onChange={onToggle} aria-label={`选中 ${v.slug}`} />
      </Td>
      <Td className="py-1.5">
        <button type="button" onClick={() => openDetail(v)} className="block" aria-label={`打开 ${v.slug} 的详情`}>
          <Thumb v={v} className="h-[54px] w-24 rounded" />
        </button>
      </Td>
      <Td className="whitespace-nowrap">
        <button type="button" onClick={() => openDetail(v)} className="hover:text-accent">
          <Code>{v.slug}</Code>
        </button>
        <div className="mt-1 flex gap-1">
          {v.status === "gone" && <Chip tone="err">已下架</Chip>}
          {v.status === "removed" && <Chip tone="err">已删源</Chip>}
          {!!v.uncensored && <Chip tone="info">无码</Chip>}
          {!v.detail_at && <Chip tone="warn">缺详情</Chip>}
        </div>
      </Td>
      <Td className="max-w-[520px]">
        <div className="truncate" title={v.title}>
          {v.title}
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-muted">
          {v.models.map(m => (
            <button key={m.id ?? m.name} type="button" className="text-ink/80 hover:text-accent" onClick={() => m.id && addFacet("model", "models", m.id, m.name)} title="只看这位女优">
              {m.name}
            </button>
          ))}
          {v.categories.slice(0, 6).map(c => (
            <button key={c.slug ?? c.name} type="button" className="hover:text-accent" onClick={() => c.slug && addFacet("category", "categories", c.slug, c.name)} title="只看这个分类">
              {c.name}
            </button>
          ))}
        </div>
      </Td>
      <Td className="whitespace-nowrap text-right text-[13px]">{fmtClockDur(v.duration)}</Td>
      <Td className="whitespace-nowrap text-[13px]">{v.release_date}</Td>
      <Td>
        <SourceChips v={v} />
      </Td>
      <Td>
        <div className="flex flex-wrap gap-1">
          {v.outputs.map(o => (
            <Chip key={o.library_id} tone={o.strm_path ? "neutral" : "warn"} title={o.strm_path || "已算进这个库，还没写出 strm（等待归入）"}>
              {o.library_name}
              {!o.strm_path && " · 待归入"}
            </Chip>
          ))}
        </div>
      </Td>
      <Td className="whitespace-nowrap text-right">
        <Button size="icon-sm" variant="ghost" onClick={() => playVideo(v)} title="试播" aria-label={`试播 ${v.slug}`}>
          <Play />
        </Button>
        <Menu>
          <MenuTrigger asChild>
            <Button size="icon-sm" variant="ghost" aria-label="更多操作">
              <MoreHorizontal />
            </Button>
          </MenuTrigger>
          <MenuContent>
            <MenuLabel>
              <Code className="text-sm">{v.slug}</Code>
            </MenuLabel>
            <MenuItem onSelect={() => openDetail(v)}>
              <PanelRight />
              详情
            </MenuItem>
            <MenuItem
              onSelect={() =>
                run(() => api.post(`/api/videos/${v.slug}/refresh`), { success: `${v.slug.toUpperCase()} 已排队刷新` }).then(() =>
                  qc.invalidateQueries({ queryKey: ["videos"] }),
                )
              }
            >
              <RefreshCw />
              刷新详情和地址
            </MenuItem>
          </MenuContent>
        </Menu>
      </Td>
    </tr>
  );
}

function VideoCard({ v, selected, onToggle }: { v: Video; selected: boolean; onToggle: () => void }) {
  return (
    <div className={cn("group relative overflow-hidden rounded-md border bg-panel", selected ? "border-accent ring-2 ring-accent/30" : "border-line")}>
      <button type="button" onClick={() => openDetail(v)} className="relative block w-full" aria-label={`打开 ${v.slug} 的详情`}>
        <Thumb v={v} className="aspect-[16/10] w-full" />
        <span className="absolute bottom-1.5 left-1.5 rounded-sm bg-black/75 px-1.5 py-1 text-white">
          <Code className="text-[14px]">{v.slug}</Code>
        </span>
        {v.duration ? <span className="absolute bottom-1.5 right-1.5 rounded-sm bg-black/65 px-1.5 py-0.5 text-xs text-white">{fmtClockDur(v.duration)}</span> : null}
        {v.status !== "active" && <span className="absolute right-1.5 top-1.5 rounded-sm bg-err px-1.5 py-0.5 text-xs text-white">{v.status === "gone" ? "已下架" : "已删源"}</span>}
      </button>
      <label
        className={cn(
          "absolute left-1.5 top-1.5 flex size-6 cursor-pointer items-center justify-center rounded bg-black/55 transition-opacity",
          selected ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-within:opacity-100",
        )}
      >
        <input type="checkbox" checked={selected} onChange={onToggle} aria-label={`选中 ${v.slug}`} />
      </label>
      <div className="space-y-1.5 p-2.5">
        <p className="line-clamp-2 text-[13px] leading-snug" title={v.title}>
          {v.title}
        </p>
        <p className="truncate text-xs text-muted">{v.models.map(m => m.name).join("、") || " "}</p>
        <div className="flex items-center gap-1.5">
          <SourceChips v={v} />
          <button type="button" onClick={() => playVideo(v)} className="ml-auto rounded p-1 text-muted hover:bg-panel-2 hover:text-accent" aria-label={`试播 ${v.slug}`}>
            <Play className="size-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}

