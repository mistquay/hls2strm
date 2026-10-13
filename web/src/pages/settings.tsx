import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Save, Search, Undo2 } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { QueryNotice } from "@/components/common";
import { navigate } from "@/lib/route";
import { useReorder } from "@/components/use-reorder";
import { Chip, Mono, Notice, Panel, Table, Td, Th } from "@/components/ui/data";
import { Check, Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { rewriteAll } from "@/lib/actions";
import { api, type LineConfig, type LineSpecMeta, type SchemaProp, type SettingsResponse, type SettingsValues, type SiteConfig } from "@/lib/api";
import { DOMAIN_MODES, PLAY_MODES, RESOLVE_MODES, VARIANT_MODES } from "@/lib/labels";
import { useMeta, useRun } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** 设置分组；note 是整组的说明，只写一次。 */
const GROUPS: { id: string; title: string; keys: string[]; note?: ReactNode }[] = [
  { id: "sites", title: "站点", keys: ["sites", "site_priority"] },
  { id: "fetch", title: "抓取与重试", keys: ["proxy", "impersonate", "request_timeout", "domain_cooldown", "domain_cooldown_max", "solver_url", "solver_timeout", "max_attempts", "retry_base_delay"] },
  { id: "tasks", title: "任务", keys: ["fetch_detail", "auto_probe_sites", "probe_recheck_days"] },
  { id: "output", title: "输出", keys: ["output_dir", "path_template", "write_nfo", "download_cover", "poster_crop"] },
  { id: "external", title: "外部整理与归档", keys: ["external_restore", "trash_days", "archive_dir"], note: <>位置同步与残留清理的计划统一在 <button className="text-accent underline" onClick={() => navigate("schedules")}>定时任务</button> 管理。</> },
  {
    id: "play",
    title: "播放地址与模式",
    keys: ["public_base_url", "play_mode", "proxy_user_agents", "play_token", "hls_margin", "play_remote"],
    note: (
      <>
        普通播放模式控制 /play；网关解析策略（见「网关」）控制 /api/resolve。网关的 Relay 只决定需要中转时由谁转发，不会强制全部资源中转。
        <a href="#/playback-guide" target="_blank" rel="noopener noreferrer" className="ml-1 text-accent underline">配置组合与路径说明（新标签页）</a>
      </>
    ),
  },
  { id: "select", title: "选源偏好", keys: ["subtitle_priority", "subtitle_fallback", "edition_fallback", "quality_first", "quality_max", "quality_unknown", "prefer_direct", "play_discover"] },
  { id: "quality", title: "画质与多画质版本", keys: ["variant_mode", "version_min_height", "quality_capture", "quality_concurrency", "quality_pending"] },
  { id: "timeouts", title: "播放时限", keys: ["resolve_timeout", "resolve_attempt_timeout"],
    note: "超过总时限还没开始返回，播放器会收到 504，日志里会写明卡在哪一步；调大只会让失败时等得更久，线路本身失败时调大没用。" },
  { id: "gateway", title: "网关 /api/resolve", keys: ["resolve_token", "resolve_mode", "resolve_relay_ttl", "resolve_proxy_url"] },
  { id: "capacity", title: "连接与缓存", keys: ["play_connections", "background_connections", "hls_connections", "file_connections", "preflight_connections", "speed_connections", "pool_wait_timeout", "relay_buffer_kb", "playlist_cache_entries", "playlist_cache_seconds", "preflight_bytes", "preflight_cache_entries", "preflight_cache_seconds", "preflight_failure_cooldown", "source_failure_cooldown", "source_failure_cooldown_max", "play_session_entries", "play_session_hours"] },
  { id: "speed", title: "线路测速", keys: ["speed_test_bytes", "speed_test_timeout"] },
  { id: "play-history", title: "播放记录", keys: ["play_history_enabled", "play_history_days", "play_history_max_entries", "play_history_pending"] },
  { id: "health", title: "连通性", keys: ["health_rank", "health_samples", "health_bytes", "health_slow_kbps"], note: <>连通性检测的执行计划在 <button className="text-accent underline" onClick={() => navigate("schedules")}>定时任务</button> 管理。</> },
];

const LABELS: Record<string, string> = {
  play_history_enabled:"记录播放交付",play_history_days:"播放记录保留天数",play_history_max_entries:"播放记录最大条数",play_history_pending:"播放记录待写入容量",
  play_remote: "按外部客户端处理普通播放",
  play_connections: "播放解析连接额度", background_connections: "后台抓取连接额度",
  hls_connections: "HLS 中转连接额度", file_connections: "文件中转连接额度",
  preflight_connections: "起播预检连接额度", speed_connections: "线路测速连接额度",
  pool_wait_timeout: "连接排队时限（秒）", relay_buffer_kb: "每连接媒体缓冲（KiB）",
  playlist_cache_entries: "清单缓存条数", playlist_cache_seconds: "清单缓存有效期（秒）",
  preflight_bytes: "起播预检采样（KiB）", preflight_cache_entries: "预检缓存条数",
  preflight_cache_seconds: "预检缓存有效期（秒）", preflight_failure_cooldown: "预检失败冷却（秒）",
  play_session_entries: "播放会话内存缓存条数", play_session_hours: "播放会话最低有效期（小时）",
  speed_test_bytes: "单线路测速采样（KiB）", speed_test_timeout: "单线路测速时限（秒）",
  source_failure_cooldown: "源 / 线路首次失败冷却（秒）", source_failure_cooldown_max: "源 / 线路冷却上限（秒）",
  quality_concurrency: "后台画质探测并发", quality_pending: "后台画质探测任务额度", domain_cooldown_max: "域名冷却上限（秒）",
  sites: "各站点",
  site_priority: "站点优先顺序",
  proxy: "上游代理（解析与中转）",
  impersonate: "浏览器指纹",
  request_timeout: "请求超时（秒）",
  domain_cooldown: "域名冷却（秒）",
  solver_url: "解题服务地址",
  solver_timeout: "解题超时（秒）",
  max_attempts: "最多尝试次数",
  retry_base_delay: "首次重试间隔（秒）",
  fetch_detail: "默认抓详情",
  auto_probe_sites: "新片自动补源",
  probe_recheck_days: "补源重查间隔（天）",
  health_rank: "按连通性挑源",
  health_interval: "定时检测间隔（分钟）",
  health_samples: "每个播放站抽几部",
  health_bytes: "每次下载多少（KB）",
  health_slow_kbps: "低于多少算慢（kbps）",
  external_restore: "外部整理库补回丢失的 strm",
  external_sync_interval: "自动同步位置间隔（分钟）",
  trash_days: "外部整理库回收区保留（天）",
  orphan_cleanup: "自动清理外部整理库残留",
  output_dir: "输出根目录",
  path_template: "默认路径模板",
  write_nfo: "写 nfo",
  download_cover: "下载封面",
  poster_crop: "裁剪 poster",
  archive_dir: "归档目录",
  public_base_url: "对外地址",
  play_mode: "普通播放模式",
  proxy_user_agents: "中转 UA 片段",
  play_token: "播放令牌",
  hls_margin: "有效期余量（分钟）",
  subtitle_priority: "字幕偏好",
  subtitle_fallback: "字幕回退",
  edition_fallback: "版本起播备用",
  quality_first: "画质优先",
  quality_max: "选源画质偏好上限",
  quality_unknown: "未知画质按",
  prefer_direct: "直连优先",
  variant_mode: "多码率的源给几档",
  version_min_height: "多画质版本最低档",
  quality_capture: "顺手探测画质",
  play_discover: "现场找源",
  resolve_timeout: "取地址总时限（秒）",
  resolve_attempt_timeout: "单源 / 线路尝试时限（秒）",
  resolve_token: "网关解析令牌",
  resolve_mode: "网关解析策略",
  resolve_relay_ttl: "中转链接期限（秒）",
  resolve_proxy_url: "公网中转地址",
};
const SUBTITLE_NAMES: Record<string, string> = { zh: "中文字幕", none: "无字幕", en: "英文字幕" };
const ENUM_NAMES: Record<string, Record<string, string>> = { edition_fallback: {off:"关闭",both:"普通版和 -U 版互为备用",normal_uses_u:"只让普通版用 -U 版备用",u_uses_normal:"只让 -U 版用普通版备用"}, play_mode: PLAY_MODES, resolve_mode: RESOLVE_MODES, variant_mode: VARIANT_MODES };
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));
const label = (k: string) => LABELS[k] ?? k;
const plain = (s: string) => s.replace(/[\s（）()，,。、:：/]/g, "").toLowerCase();

/** 离开设置页时没保存的改动：回来时接着改，不丢。只在这一次打开的页面里有效。 */
let unsaved: SettingsValues | null = null;

/** 「sites.jable.domains：原因；proxy：原因」→ {sites: "jable.domains：原因", proxy: "原因"} */
function fieldErrors(message: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of message.split("；")) {
    const m = part.match(/^([a-z_]+)((?:\.[\w-]+)*)：(.+)$/);
    if (m) out[m[1]] = [out[m[1]], (m[2] ? m[2].slice(1) + "：" : "") + m[3]].filter(Boolean).join("；");
  }
  return out;
}

/** 默认值的写法：开关写开/关，选项写名字，空串写「空」。 */
function showDefault(k: string, schema: SchemaProp): string {
  const d = schema.default;
  if (typeof d === "boolean") return d ? "开" : "关";
  if (d === "" || d == null) return "空";
  if (Array.isArray(d)) return d.length ? d.map(x => (k === "subtitle_priority" ? SUBTITLE_NAMES[x] ?? x : String(x))).join("、") : "空";
  if (typeof d === "object") return "默认配置";
  return ENUM_NAMES[k]?.[String(d)] ?? String(d);
}

export default function Settings() {
  const qc = useQueryClient();
  const run = useRun();
  const meta = useMeta();
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: ({ signal }) => api.get<SettingsResponse>("/api/settings", signal), staleTime: Infinity,
    refetchInterval:q=>q.state.data?.output_sync?.job_id && ["running","paused"].includes(q.state.data.output_sync.status) ? 3000 : false });
  const data = settingsQuery.data;
  const [draft, setDraft] = useState<SettingsValues | null>(null);
  const [restored, setRestored] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [q, setQ] = useState("");
  const latest = useRef<{ draft: SettingsValues | null; values: SettingsValues | null }>({ draft: null, values: null });
  useEffect(() => {
    if (!data) return;
    setRestored(!!unsaved && !same(unsaved, data.values));
    const previous = latest.current;
    const edits = previous.draft && previous.values ? Object.fromEntries(Object.keys(previous.draft).filter(k=>!same(previous.draft![k],previous.values![k])).map(k=>[k,previous.draft![k]])) : {};
    setDraft(clone(unsaved && !same(unsaved, data.values) ? unsaved : {...data.values,...edits}));
    unsaved = null;
  }, [JSON.stringify(data?.values)]);
  latest.current = { draft, values: data?.values ?? null };
  const dirty = !!draft && !!data && !same(draft, data.values);
  useEffect(() => () => {
    const { draft: d, values } = latest.current;
    unsaved = d && values && !same(d, values) ? d : null;
  }, []);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault(); // 关页面、刷新前提醒还有没保存的改动
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  if (!data || !draft) return <QueryNotice query={settingsQuery} label="设置" />;

  const changed = Object.keys(draft).filter(k => !same(draft[k], data.values[k]));
  const set = (k: string, v: unknown) => {
    setDraft(d => (d ? { ...d, [k]: v } : d));
    if (errors[k]) setErrors(({ [k]: _, ...rest }) => rest);
  };
  const needle = q.trim().toLowerCase();
  const siteWords = (name: string) => {
    const cfg = draft.sites[name];
    return [name, meta.site(name).label, ...cfg.domains, ...meta.site(name).lines.map(l => `${l.name} ${l.host_label}`)].join(" ").toLowerCase();
  };
  const matchSites = Object.keys(draft.sites).filter(name => !needle || siteWords(name).includes(needle));
  const matches = (k: string) => {
    if (!needle) return true;
    if (k === "sites") return matchSites.length > 0 || "站点 镜像 域名 线路 限速 并发 解题".includes(needle);
    return [label(k), k, data.schema[k]?.description ?? ""].join(" ").toLowerCase().includes(needle);
  };
  const groups = GROUPS.map(g => ({ ...g, keys: g.keys.filter(k => data.schema[k] && matches(k)) })).filter(g => g.keys.length);

  const save = async () => {
    const patch = Object.fromEntries(changed.map(k => [k, draft[k]]));
    const r = await run(async () => {
      try {
        return await api.put<Pick<SettingsResponse, "values" | "effective" | "output_sync">>("/api/settings", patch);
      } catch (e) {
        const found = fieldErrors((e as Error).message);
        setErrors(found);
        const first = GROUPS.flatMap(g => g.keys).find(k => found[k]);
        if (first) document.getElementById(`setting-${first}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
        throw Object.keys(found).length ? new Error(Object.entries(found).map(([k, msg]) => `${label(k)}：${msg}`).join("；")) : e;
      }
    }, { success: "服务设置已保存，已有输出的同步状态见下方", invalidate: [["status"], ["meta"]] });
    if (!r) return;
    setErrors({});
    setRestored(false);
    qc.setQueryData<SettingsResponse>(["settings"], prev => (prev ? { ...prev, values: r.values, effective: r.effective, output_sync:r.output_sync } : prev));
  };
  const discard = () => {
    setDraft(clone(data.values));
    setErrors({});
    setRestored(false);
  };

  return (
    <div className="xl:grid xl:grid-cols-[170px_minmax(0,1fr)] xl:gap-6">
      <nav className="sticky top-16 hidden h-fit space-y-0.5 xl:block" aria-label="设置分组">
        {groups.map(g => (
          <a key={g.id} href={`#/settings`} onClick={e => { e.preventDefault(); document.getElementById(`set-${g.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); }} className="block rounded-md px-2.5 py-1.5 text-sm text-muted hover:bg-panel-2 hover:text-ink">
            {g.title}
            {g.keys.some(k => changed.includes(k)) && <span className="ml-1.5 inline-block size-1.5 rounded-full bg-accent align-middle" title="有没保存的改动" />}
            {g.keys.some(k => errors[k]) && <span className="ml-1.5 inline-block size-1.5 rounded-full bg-err align-middle" title="有错误" />}
          </a>
        ))}
      </nav>
      <div className="space-y-4">
        <div className="sticky top-12 z-20 -mx-3 flex flex-wrap items-center gap-2 border-b border-line bg-bg/95 px-3 py-2.5 backdrop-blur sm:-mx-5 sm:px-5">
          <Button variant="primary" onClick={save} disabled={!changed.length || run.busy}>
            <Save />
            保存{changed.length ? `（${changed.length} 项改动）` : ""}
          </Button>
          <Button onClick={discard} disabled={!changed.length || run.busy}>
            <Undo2 />
            放弃改动
          </Button>
          <div className="relative w-full sm:w-64">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted" />
            <Input className="w-full pl-8" placeholder="搜设置：名称、说明、站点、域名" value={q} onChange={e => setQ(e.target.value)} aria-label="搜索设置" />
          </div>
          <span className="ml-auto text-[13px] text-muted">
            生效的输出目录 <Mono>{data.effective.output_dir}</Mono>，归档目录 <Mono>{data.effective.archive_dir}</Mono>，对外地址 <Mono>{data.effective.public_base_url}</Mono>
          </span>
        </div>
        {restored && (
          <Notice tone="info" actions={<Button size="sm" onClick={discard}>不要了</Button>}>
            接着上次没保存的 {changed.length} 项改动，确认后点「保存」。
          </Notice>
        )}
        <QueryNotice query={settingsQuery} label="设置" />
        {data.output_sync && (
          <Notice
            actions={
              <>
              {data.output_sync.job_id && <Button size="sm" onClick={()=>navigate("jobs",{job:String(data.output_sync!.job_id)})}>查看同步任务 #{data.output_sync.job_id}</Button>}
              {!["running","paused","synced"].includes(data.output_sync.status) && <Button size="sm" variant="primary" disabled={run.busy} onClick={() => rewriteAll(run).then(() => qc.invalidateQueries({queryKey:["settings"]}))}>
                重写全部输出
              </Button>}
              </>
            }
          >
            <p>服务设置已保存 · {({pending:"已有输出待同步",running:"输出同步进行中",paused:"同步任务已暂停",cancelled:"同步已取消，已有输出仍待同步",partial_failure:"输出同步部分失败，请查看任务",synced:"已有输出同步完成"} as Record<string,string>)[data.output_sync.status]}</p>
            <p className="mt-1 text-xs">影响：{data.output_sync.keys.map(label).join("、")}。普通库按模板更新或移动文件；外部整理库仅原地修改 STRM，找不到的文件按现有保护策略处理。</p>
          </Notice>
        )}
        {!groups.length && <div className="py-16 text-center text-sm text-muted">没有和「{q}」相关的设置</div>}
        <fieldset disabled={run.busy} className="space-y-4">
        {groups.map(g => (
          <Panel key={g.id} id={`set-${g.id}`} title={g.title} bodyClassName="divide-y divide-line py-0" className="scroll-mt-28">
            {g.note && <p className="py-3 text-[13px] leading-relaxed text-muted">{g.note}</p>}
            {g.keys.map(k => (
              <Setting key={k} k={k} schema={data.schema[k]} value={draft[k]} saved={data.values[k]} error={errors[k]}
                onRevert={() => set(k, clone(data.values[k]))} onDefault={() => set(k, clone(data.schema[k].default))}>
                <Control k={k} schema={data.schema[k]} value={draft[k]} draft={draft} saved={data.values} set={set}
                  only={needle ? matchSites : undefined} />
              </Setting>
            ))}
          </Panel>
        ))}
        </fieldset>
      </div>
    </div>
  );
}

function Setting({ k, schema, value, saved, error, onRevert, onDefault, children }: {
  k: string; schema: SchemaProp; value: unknown; saved: unknown; error?: string;
  onRevert: () => void; onDefault: () => void; children: ReactNode;
}) {
  const wide = k === "sites";
  const changed = !same(value, saved);
  const hasDefault = Array.isArray(schema.default) || (schema.default !== undefined && typeof schema.default !== "object");
  const notDefault = hasDefault && !same(value, schema.default);
  const desc = schema.description && plain(schema.description) !== plain(label(k)) ? schema.description : ""; // 和标题一字不差的说明不显示
  return (
    <div id={`setting-${k}`} className={cn("grid scroll-mt-32 gap-x-6 gap-y-2 py-4", !wide && "md:grid-cols-[240px_minmax(0,1fr)]")}>
      <div className={cn("min-w-0", changed && "border-l-2 border-accent pl-2.5", error && "border-l-2 border-err pl-2.5")}>
        <div className="text-sm font-medium" title={`配置键 ${k}`}>{label(k)}</div>
        {!wide && (changed || notDefault) && (
          <div className="mt-1 flex flex-wrap gap-x-3 text-xs">
            {changed && <button type="button" className="text-accent hover:underline" onClick={onRevert}>撤销改动</button>}
            {notDefault && (
              <button type="button" className="text-muted hover:text-ink hover:underline" onClick={onDefault} title={`恢复成默认值：${showDefault(k, schema)}`}>
                恢复默认（{showDefault(k, schema)}）
              </button>
            )}
          </div>
        )}
      </div>
      <div className="min-w-0 space-y-1.5">
        {children}
        {error && <p className="text-[13px] text-err">{error}</p>}
        {desc && <p className="max-w-[80ch] text-[13px] leading-relaxed text-muted">{desc}</p>}
      </div>
    </div>
  );
}

function Control({ k, schema, value, draft, saved, set, only }: {
  k: string; schema: SchemaProp; value: unknown; draft: SettingsValues; saved: SettingsValues;
  set: (k: string, v: unknown) => void; only?: string[];
}) {
  const meta = useMeta();
  if (k === "sites") return <SitesEditor sites={draft.sites} saved={saved.sites} only={only} onChange={v => set("sites", v)} />;
  if (k === "site_priority") return <OrderList items={value as string[]} label={meta.label} onChange={v => set(k, v)} />;
  if (k === "subtitle_priority") return <OrderList items={value as string[]} label={x => SUBTITLE_NAMES[x] ?? x} onChange={v => set(k, v)} />;
  if (k === "auto_probe_sites") {
    const v = value as string[];
    return (
      <div className="flex flex-wrap gap-x-5 gap-y-1.5">
        {Object.entries(meta.sites).map(([name, m]) => (
          <Check key={name} checked={v.includes(name)} onChange={on => set(k, on ? [...v, name] : v.filter(x => x !== name))}>
            {m.label}
          </Check>
        ))}
      </div>
    );
  }
  if (schema.enum)
    return (
      <Select value={String(value)} aria-label={label(k)} onChange={e => set(k, e.target.value)} className="w-full max-w-md">
        {schema.enum.map(o => (
          <option key={o} value={o}>
            {ENUM_NAMES[k]?.[o] ?? o}
          </option>
        ))}
      </Select>
    );
  if (schema.type === "boolean") return <Switch aria-label={LABELS[k] ?? k} checked={!!value} onCheckedChange={v => set(k, v)} />;
  if (schema.type === "integer" || schema.type === "number")
    return (
      <Input
        type="number"
        aria-label={LABELS[k] ?? k}
        className="w-36"
        step={schema.type === "integer" ? 1 : 0.1}
        min={schema.minimum ?? schema.exclusiveMinimum}
        max={schema.maximum}
        value={value as number}
        onChange={e => set(k, e.target.value === "" ? "" : Number(e.target.value))}
      />
    );
  if (schema.type === "array") return <Lines value={value as string[]} onChange={v => set(k, v)} />;
  return (
    <>
      <Input aria-label={LABELS[k] ?? k} value={String(value ?? "")} onChange={e => set(k, e.target.value)} className="max-w-xl" />
      {k === "solver_url" && <SolverTest url={String(value ?? "")} />}
    </>
  );
}

/** 每行一个值的列表。 */
function Lines({ value, onChange, className }: { value: string[]; onChange: (v: string[]) => void; className?: string }) {
  const [text, setText] = useState(value.join("\n"));
  useEffect(() => {
    if (!same(text.split("\n").map(x => x.trim()).filter(Boolean), value)) setText(value.join("\n"));
  }, [value]);
  return (
    <Textarea
      className={cn("max-w-xl min-h-0", className)}
      rows={Math.max(2, text.split("\n").length)}
      value={text}
      onChange={e => {
        setText(e.target.value);
        onChange(e.target.value.split("\n").map(x => x.trim()).filter(Boolean));
      }}
    />
  );
}

/** 优先顺序在保存设置前只修改本地草稿。 */
function OrderList({ items, label, onChange }: { items: string[]; label: (x: string) => string; onChange: (v: string[]) => void }) {
  const reorder = useReorder(items, onChange);
  return (
    <div ref={reorder.root}>
    <p className="mb-1 text-xs text-muted">拖动手柄调整顺序，前面的优先；保存后生效。</p>
    <ol className="w-full max-w-sm divide-y divide-line rounded-md border border-line">
      {items.map((x, i) => (
        <li key={x} {...reorder.row(x)} className={cn("flex items-center gap-2 px-2.5 py-1.5 text-sm", reorder.row(x).className)}>
          {reorder.handle(x, label(x))}
          <span className="w-4 text-xs text-muted">{i + 1}</span>
          <span className="flex-1">{label(x)}</span>
        </li>
      ))}
    </ol>
    </div>
  );
}

const OPEN_KEY = "settings.sites.open";
const loadOpen = (): string[] => {
  try {
    return JSON.parse(localStorage.getItem(OPEN_KEY) ?? "[]");
  } catch {
    return [];
  }
};

/** 各站点：默认收起，标题行显示摘要和常用开关；展开的站点记在本浏览器里。搜索时只显示匹配的站点并展开。 */
function SitesEditor({ sites, saved, only, onChange }: {
  sites: Record<string, SiteConfig>; saved: Record<string, SiteConfig>; only?: string[];
  onChange: (v: Record<string, SiteConfig>) => void;
}) {
  const meta = useMeta();
  const [open, setOpen] = useState<string[]>(loadOpen);
  const keep = (v: string[]) => {
    setOpen(v);
    try {
      localStorage.setItem(OPEN_KEY, JSON.stringify(v));
    } catch {
      /* 存不了就只在这次有效 */
    }
  };
  const update = (name: string, patch: Partial<SiteConfig>) => onChange({ ...sites, [name]: { ...sites[name], ...patch } });
  const names = Object.keys(sites).filter(name => !only || only.includes(name));
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start gap-2">
        <p className="max-w-[90ch] flex-1 text-xs leading-relaxed text-muted">
          镜像域名是站点页面的镜像，抓取和播放解析共用所选的域名策略；站点总并发和限速不变，视频流量走哪个 CDN 由线路决定，只填同一站点的有效镜像。
          线路是外部客户端的播放策略：中转经过本服务、保持原媒体编码；「允许外部直连」要自己验证，解除不了 CDN 的出口限制。
        </p>
        <Button size="sm" variant="ghost" onClick={() => keep(names)}>全部展开</Button>
        <Button size="sm" variant="ghost" onClick={() => keep([])}>全部收起</Button>
      </div>
      {names.map(name => {
        const cfg = sites[name];
        const m = meta.site(name);
        const expanded = !!only || open.includes(name);
        const lineNames = cfg.line_order.length ? cfg.line_order : m.lines.map(l => l.name);
        const usable = lineNames.filter(n => (cfg.lines[n]?.enabled ?? true) && (m.lines.find(l => l.name === n)?.supported ?? true)).length;
        const summary = [
          `${cfg.domains.length} 个镜像`,
          DOMAIN_MODES[cfg.domain_mode]?.replace(/（.*）/, ""),
          `限速 ${cfg.rate_per_sec}/秒`,
          `抓取并发 ${cfg.concurrency}，播放并发 ${cfg.play_concurrency}`,
          m.lines.length ? `线路 ${usable}/${lineNames.length} 可用` : "",
        ].filter(Boolean).join(" · ");
        return (
          <div key={name} className={cn("min-w-0 rounded-md border border-line", !cfg.enabled && "bg-panel-2/60")}>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2.5">
              <button type="button" className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={() => keep(expanded ? open.filter(x => x !== name) : [...open, name])}
                aria-expanded={expanded} aria-label={`${expanded ? "收起" : "展开"} ${m.label}`}>
                {expanded ? <ChevronDown className="size-4 shrink-0 text-muted" /> : <ChevronRight className="size-4 shrink-0 text-muted" />}
                <b className="text-[15px]">{m.label}</b>
                <Chip>{m.lines.length ? "按线路策略播放" : m.direct ? (m.ip_bound ? "302（直链绑出口 IP，网关不用它）" : "可以 302") : "必须中转"}</Chip>
                {!same(cfg, saved[name]) && <span className="size-1.5 shrink-0 rounded-full bg-accent" title="有没保存的改动" />}
                {!expanded && <span className="min-w-0 truncate text-xs text-muted">{summary}</span>}
              </button>
              <label className="flex cursor-pointer items-center gap-2 text-[13px]">
                <Switch checked={cfg.enabled} onCheckedChange={v => update(name, { enabled: v })} aria-label={`启用 ${m.label}`} />
                启用
              </label>
              <label className="flex cursor-pointer items-center gap-2 text-[13px]">
                <Switch checked={cfg.solver} onCheckedChange={v => update(name, { solver: v })} aria-label={`${m.label} 被拦时用解题服务`} />
                被拦时用解题服务
              </label>
            </div>
            {expanded && (
              <div className="border-t border-line p-3">
                <div className="mb-3 grid gap-3 sm:grid-cols-[minmax(0,1fr)_160px]">
                  <Field label="域名选择策略">
                    <Select value={cfg.domain_mode} aria-label={`${m.label} 域名选择策略`} onChange={e => update(name, { domain_mode: e.target.value as SiteConfig["domain_mode"] })}>
                      {Object.entries(DOMAIN_MODES).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
                    </Select>
                  </Field>
                  <Field label="网络错误冷却（秒）" hint={cfg.domain_mode === "priority" ? "按顺序优先时不用" : "0 关闭"}>
                    <Input type="number" min={0} aria-label={`${m.label} 网络错误冷却（秒）`} disabled={cfg.domain_mode === "priority"} value={cfg.domain_error_cooldown} onChange={e => update(name, { domain_error_cooldown: Number(e.target.value) })} />
                  </Field>
                </div>
                <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_100px_100px_100px]">
                  <Field label="镜像域名（每行一个，顺序作为优先 / 平局依据）">
                    <Lines value={cfg.domains} onChange={v => update(name, { domains: v })} />
                  </Field>
                  <Field label="限速（次/秒）">
                    <Input type="number" step={0.1} min={0.05} value={cfg.rate_per_sec} onChange={e => update(name, { rate_per_sec: Number(e.target.value) })} />
                  </Field>
                  <Field label="后台抓取并发">
                    <Input type="number" min={1} value={cfg.concurrency} onChange={e => update(name, { concurrency: Number(e.target.value) })} />
                  </Field>
                  <Field label="播放页面并发">
                    <Input type="number" min={1} value={cfg.play_concurrency} onChange={e => update(name, { play_concurrency: Number(e.target.value) })} />
                  </Field>
                </div>
                {m.lines.length > 0 && <LinesTable specs={m.lines} cfg={cfg} onChange={patch => update(name, patch)} />}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function LinesTable({ specs, cfg, onChange }: { specs: LineSpecMeta[]; cfg: SiteConfig; onChange: (patch: Partial<SiteConfig>) => void }) {
  const order = cfg.line_order.length ? cfg.line_order : specs.map(s => s.name);
  const spec = (name: string): LineSpecMeta =>
    specs.find(s => s.name === name) ?? { name, host: "", host_label: "", note: "站点上新出现的线路，按页面内容识别播放站", supported: true, direct: true, ip_bound: false };
  const lineCfg = (name: string): LineConfig => cfg.lines[name] ?? { enabled: true, proxy: false };
  const setLine = (name: string, patch: Partial<LineConfig>) => onChange({ lines: { ...cfg.lines, [name]: { ...lineCfg(name), ...patch } }, line_order: order });
  const reorder = useReorder(order, line_order => onChange({ line_order }));
  const mode = (s: LineSpecMeta, c: LineConfig) => (!s.supported ? "暂不支持" : !s.direct || c.proxy || c.direct_mode === "proxy" ? "原样中转" : c.direct_mode === "allow" ? "允许外部直连（手动覆盖）" : s.ip_uncertain ? "出口限制待验证 → 中转" : s.ip_bound ? "直链受出口限制 → 中转" : "外部可直连");
  return (
    <div className="mt-3" ref={reorder.root}>
      <div className="mb-1 text-[13px] text-muted">线路（拖动手柄调整尝试顺序，保存后生效）</div>
      <Table className="text-[13px]">
        <thead>
          <tr>
            <Th>顺序</Th>
            <Th>线路</Th>
            <Th>播放站</Th>
            <Th>外部播放</Th>
            <Th>启用</Th>
            <Th>直连策略</Th>
          </tr>
        </thead>
        <tbody>
          {order.map((name, i) => {
            const s = spec(name);
            const c = lineCfg(name);
            return (
              <tr key={name} {...reorder.row(name)} className={cn(reorder.row(name).className, (!c.enabled || !s.supported) && "opacity-55")} title={s.note}>
                <Td className="whitespace-nowrap">
                  {reorder.handle(name, `线路 ${name}`)}
                  <span className="ml-1 text-xs text-muted">{i + 1}</span>
                </Td>
                <Td className="font-mono">{name}</Td>
                <Td>
                  {s.host_label || "-"}
                  {s.note && <div className="max-w-[260px] truncate text-xs text-muted">{s.note}</div>}
                </Td>
                <Td className="whitespace-nowrap">
                  <Chip>{mode(s, c)}</Chip>
                </Td>
                <Td>
                  <Switch checked={c.enabled} disabled={!s.supported} onCheckedChange={v => setLine(name, { enabled: v })} aria-label={`启用线路 ${name}`} />
                </Td>
                <Td>
                  <Select value={c.proxy ? "proxy" : c.direct_mode || "auto"} disabled={!s.direct || !s.supported}
                    aria-label={`线路 ${name} 直连策略`} onChange={e => setLine(name, {
                      proxy: e.target.value === "proxy", direct_mode: e.target.value as LineConfig["direct_mode"],
                    })}>
                    <option value="auto">自动（受限时中转）</option><option value="allow">允许外部直连</option><option value="proxy">强制原样中转</option>
                  </Select>
                </Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}

interface SolverResult {
  ok: boolean;
  error?: string;
  status?: number;
  ms?: number;
  cookies?: number;
  service?: string;
  version?: string;
  warning?: string;
}

function SolverTest({ url }: { url: string }) {
  const meta = useMeta();
  const [site, setSite] = useState("jable");
  const [state, setState] = useState<{ busy: boolean; mode: "ping" | "solve"; result: SolverResult | null }>({ busy: false, mode: "ping", result: null });
  const test = async (mode: "ping" | "solve") => {
    setState({ busy: true, mode, result: null });
    try {
      const result = await api.post<SolverResult>("/api/solver/test", { url, mode, site });
      setState({ busy: false, mode, result });
    } catch (e) {
      setState({ busy: false, mode, result: { ok: false, error: (e as Error).message } });
    }
  };
  const r = state.result;
  const ms = r?.ms != null ? (r.ms >= 1000 ? `${(r.ms / 1000).toFixed(1)} 秒` : `${r.ms} ms`) : "";
  let text = "";
  if (r) {
    if (r.error) text = (state.mode === "solve" ? "解题失败：" : "连不上：") + r.error;
    else if (state.mode === "solve") text = r.ok ? `通过挑战：HTTP ${r.status}，拿到 cookie ${r.cookies} 个（${ms}）` : `没通过挑战：HTTP ${r.status}（${ms}），可能要换代理`;
    else if (!r.ok) text = `能连上，但服务出错：HTTP ${r.status}`;
    else text = `已连上${[r.service, r.version].filter(Boolean).length ? "：" + [r.service, r.version].filter(Boolean).join(" ") : ""}（${ms}）${r.warning ? "。" + r.warning : ""}`;
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" onClick={() => test("ping")} disabled={state.busy || !url}>
        测试连通
      </Button>
      <Select value={site} onChange={e => setSite(e.target.value)} className="w-28" aria-label="试解哪个站点">
        {Object.entries(meta.sites).map(([k, m]) => (
          <option key={k} value={k}>
            {m.label}
          </option>
        ))}
      </Select>
      <Button size="sm" onClick={() => test("solve")} disabled={state.busy || !url} title="让解题服务实际打开一次所选站点的首选域名（走当前代理），最长等到解题超时">
        试解一次
      </Button>
      {state.busy && <span className="text-[13px] text-muted">{state.mode === "solve" ? "解题中，最长等到解题超时…" : "检测中…"}</span>}
      {r && !state.busy && <span className={cn("text-[13px]", r.ok ? "text-ok" : "text-err")}>{text}</span>}
    </div>
  );
}
