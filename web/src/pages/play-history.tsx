import { useQuery } from "@tanstack/react-query";
import { RotateCw, Search } from "lucide-react";
import { Pager, QueryNotice } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Chip, EmptyRow, Mono, Notice, Panel, Table, Td, Th, type Tone } from "@/components/ui/data";
import { Field, Input, Select } from "@/components/ui/form";
import { api, qs, type Page } from "@/lib/api";
import { fmtNum, fmtTime } from "@/lib/format";
import { useMeta } from "@/lib/queries";
import { navigate, replaceParams, useRoute } from "@/lib/route";

interface PlaybackRecord {
  id:number;created_at:number;updated_at:number;slug:string;code:string;title:string;
  video_id:number|null;source_id:number|null;site:string;source_key:string;line:string;host:string;
  mode:"proxy"|"redirect"|"";entry:"play"|"session"|"gateway";
  state:"issued"|"delivered"|"failed"|"interrupted";http_status:number;client:string;user_agent:string;request_id:string;
}
interface HistoryPage extends Page<PlaybackRecord> {
  counts:{total:number;proxy:number;redirect:number;failed:number};
  enabled:boolean;dropped:number;retention_days:number;max_entries:number;
}
const STATES:Record<PlaybackRecord["state"],[string,Tone]> = {issued:["地址已返回","info"],delivered:["已交付","ok"],failed:["交付失败","err"],interrupted:["传输中断","warn"]};
const ENTRIES:Record<PlaybackRecord["entry"],string> = {play:"播放地址",session:"会话播放",gateway:"网关解析"};

function boundary(value:string,next=false) {
  if(!value) return undefined;
  const [y,m,d] = value.split("-").map(Number);
  return Math.floor(new Date(y,m-1,d+(next ? 1 : 0)).getTime()/1000);
}

export default function PlayHistory() {
  const {params} = useRoute();
  const meta = useMeta();
  const q = params.get("q") ?? "",mode=params.get("mode") ?? "",site=params.get("site") ?? "",state=params.get("state") ?? "";
  const from=params.get("from") ?? "",to=params.get("to") ?? "";
  const page=Math.max(1,Number(params.get("page"))||1);
  const filter={q,mode,site,state,since:boundary(from),until:boundary(to,true),limit:50,offset:(page-1)*50};
  const query=useQuery({queryKey:["play-history",filter],queryFn:({signal})=>api.get<HistoryPage>("/api/play-history"+qs(filter),signal),refetchInterval:10000});
  const set=(key:string,value:string)=>{
    const next=new URLSearchParams(params);
    if(value) next.set(key,value);else next.delete(key);
    if(key!=="page") next.delete("page");
    replaceParams(next);
  };
  const data=query.data;
  return <div className="space-y-4">
    <p className="text-sm text-muted">按实际选中的站点记录中转或 302。记录表示地址、清单或媒体已交付；302 和网关返回地址后，无法确认客户端是否起播或完整观看。分片、HEAD 探测不单独记播放。</p>
    {data && !data.enabled && <Notice>当前已暂停记录新播放，历史仍可查看。<Button variant="ghost" onClick={()=>navigate("settings")}>前往设置</Button></Notice>}
    {!!data?.dropped && <Notice>本次运行有 {fmtNum(data.dropped)} 次记录因待写入队列满而跳过，播放交付继续。可在设置中调整容量。</Notice>}
    <div className="flex flex-wrap items-end gap-3">
      <div className="relative min-w-52 flex-1"><Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted" /><Input className="pl-9" value={q} onChange={e=>set("q",e.target.value)} aria-label="搜索播放影片" placeholder="搜番号、标题或影片标识" /></div>
      <Field label="交付方式"><Select value={mode} onChange={e=>set("mode",e.target.value)} aria-label="筛选交付方式"><option value="">全部方式</option><option value="proxy">原样中转</option><option value="redirect">302 直连</option></Select></Field>
      <Field label="实际站点"><Select value={site} onChange={e=>set("site",e.target.value)} aria-label="筛选播放站点"><option value="">全部站点</option>{Object.entries(meta.sites).map(([key,value])=><option key={key} value={key}>{value.label}</option>)}</Select></Field>
      <Field label="交付状态"><Select value={state} onChange={e=>set("state",e.target.value)} aria-label="筛选交付状态"><option value="">全部状态</option>{Object.entries(STATES).map(([key,[label]])=><option key={key} value={key}>{label}</option>)}</Select></Field>
      <Field label="开始日期"><Input type="date" value={from} onChange={e=>set("from",e.target.value)} aria-label="开始日期" /></Field>
      <Field label="结束日期"><Input type="date" value={to} onChange={e=>set("to",e.target.value)} aria-label="结束日期" /></Field>
      <Button onClick={()=>replaceParams(new URLSearchParams())}>清空筛选</Button>
      <Button onClick={()=>query.refetch()} disabled={query.isFetching}><RotateCw />刷新</Button>
    </div>
    <QueryNotice query={query} label="播放记录" />
    {data && <p className="text-sm text-muted">当前范围 {fmtNum(data.total)} 条 · 原样中转 {fmtNum(data.counts.proxy)} · 302 直连 {fmtNum(data.counts.redirect)} · 失败/中断 {fmtNum(data.counts.failed)}{data.retention_days ? ` · 保留 ${data.retention_days} 天` : ""}{data.max_entries ? ` · 最多 ${fmtNum(data.max_entries)} 条` : ""}</p>}
    <Panel title="播放记录">
      <Table><thead><tr><Th>时间</Th><Th>影片</Th><Th>方式</Th><Th>实际站点 / 线路</Th><Th>交付状态</Th><Th>入口 / 客户端</Th></tr></thead><tbody>
        {(data?.items ?? []).map(row=><tr key={row.id}>
          <Td className="whitespace-nowrap">{fmtTime(row.created_at)}</Td>
          <Td><button className="text-left hover:text-accent" onClick={()=>row.slug && navigate("videos",{q:row.slug})} disabled={!row.slug}><Mono>{row.code || row.slug || "影片未知"}</Mono><p className="mt-1 max-w-64 truncate text-xs text-muted" title={row.title}>{row.title || row.slug || "会话已失效"}</p></button></Td>
          <Td><Chip tone={row.mode==="proxy" ? "warn" : "info"}>{row.mode==="proxy" ? "原样中转" : row.mode==="redirect" ? "302 直连" : "未确定"}</Chip></Td>
          <Td><span>{row.site ? meta.label(row.site) : "尚未选源"}</span><p className="mt-1 text-xs text-muted">{[row.line,row.source_key].filter(Boolean).join(" · ")}</p></Td>
          <Td><Chip tone={STATES[row.state][1]}>{STATES[row.state][0]}</Chip><p className="mt-1 text-xs text-muted" title={row.request_id ? `请求编号 ${row.request_id}` : undefined}>HTTP {row.http_status}</p></Td>
          <Td><span className="text-xs">{ENTRIES[row.entry]} · {row.client || "未知地址"}</span><p className="mt-1 max-w-48 truncate text-xs text-muted" title={row.user_agent}>{row.user_agent || "未知客户端"}</p></Td>
        </tr>)}
        {!data?.items.length && !query.isPending && !query.error && <EmptyRow cols={6}>{q || mode || site || state || from || to ? "没有符合筛选的播放记录" : "还没有播放记录，新播放会从现在开始记录。"}</EmptyRow>}
      </tbody></Table>
    </Panel>
    <Pager page={page} size={50} total={data?.total ?? 0} onPage={value=>set("page",String(value))} />
  </div>;
}
