import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { Button } from "./ui/button";
import { Check, Select } from "./ui/form";
import type { OperationRequest } from "./operation-preview";

interface Member {
  id:number;slug:string;title:string;status:string;plays:number;
  sources:{site:string;status:string}[];
  outputs:{library_id:number;library_name:string;strm_path:string;paths:string[];external_paths:string[]}[];
}
export interface DedupeGroup {
  group_id:number;code_key:string;uncensored:boolean;keep:number;members:Member[];
  conflicts:{video_ids:number[];reasons:string[]}[];selected:boolean;changes_library:boolean;
  predicted_libraries:number[];shared_dirs:string[];missing:{slug:string;path:string}[];
}

export function DedupePreview({jobId,request,attention,names,onChange,disabled}:{jobId:number;request:OperationRequest;attention:DedupeGroup[];names:Record<string,string>;onChange?:(request:OperationRequest)=>void;disabled:boolean}) {
  const [page,setPage] = useState(0);
  const query = useQuery({queryKey:["dedupe-groups",jobId,page],queryFn:({signal})=>api.get<{items:DedupeGroup[];total:number}>(`/api/operations/${jobId}/groups?offset=${page*20}&limit=20`,signal)});
  const update = (key:string,value:unknown)=>onChange?.({...request,payload:{...request.payload,[key]:value}});
  const toggle = (key:string,id:number,on:boolean)=>{
    const ids = (request.payload[key] as number[] | undefined) ?? [];
    update(key,on ? [...new Set([...ids,id])] : ids.filter(i=>i!==id));
  };
  const group = (g:DedupeGroup)=><div key={g.group_id} className="space-y-2 rounded border border-line p-3 text-xs">
    <b>{g.code_key} · {g.uncensored ? "-U 版" : "普通版"} · {g.selected ? "将执行" : "已跳过"}</b>
    {!!g.conflicts.length && <p className="text-warn">疑似不同片：{g.conflicts.map(c=>`影片 ${c.video_ids.join(" / ")}：${c.reasons.join("、")}`).join("；")}</p>}
    <div className="flex flex-wrap items-center gap-3">
      <label>保留项 <Select aria-label={`组 ${g.group_id} 保留项`} value={g.keep} disabled={disabled || !onChange} onChange={e=>update("keep",{...(request.payload.keep as Record<string,number> ?? {}),[g.group_id]:Number(e.target.value)})}>{g.members.map(m=><option key={m.id} value={m.id}>{m.slug}</option>)}</Select></label>
      <Check checked={((request.payload.skip as number[]) ?? []).includes(g.group_id)} disabled={disabled || !onChange} onChange={v=>toggle("skip",g.group_id,v)}>跳过本组</Check>
      {!!g.conflicts.length && <Check checked={((request.payload.allow_conflicts as number[]) ?? []).includes(g.group_id)} disabled={disabled || !onChange} onChange={v=>toggle("allow_conflicts",g.group_id,v)}>确认冲突组为同片</Check>}
    </div>
    {g.members.map(m=><div key={m.id} className="rounded bg-panel-2 p-2">
      <b>{m.id===g.keep ? "保留" : "并入"}：{m.slug}</b> · {m.title} · {m.status} · 播放 {m.plays} 次
      <p className="text-muted">源：{m.sources.map(s=>`${s.site}（${s.status}）`).join("、") || "无"}</p>
      {m.outputs.map(o=><p key={o.library_id} className="break-all">库「{o.library_name}」：{o.paths.length ? o.paths.map(p=>`${p}${o.external_paths.includes(p) ? "（整理目录）" : ""}`).join("；") : o.strm_path ? `${o.strm_path}（未找到）` : "未写出"}</p>)}
      {!m.outputs.length && <p>未归入输出库</p>}
    </div>)}
    <p className={g.changes_library ? "text-warn" : "text-muted"}>{g.changes_library ? "预计换库" : "预计归属"}：{g.predicted_libraries.map(id=>names[id] ?? id).join("、") || "无"}</p>
    {!!g.shared_dirs.length && <p>共享目录的其他元数据保留：{g.shared_dirs.join("、")}</p>}
    {!!g.missing.length && <p>未找到的输出保持原状，旧播放链接继续兼容：{g.missing.map(m=>m.path).join("、")}</p>}
  </div>;
  return <div className="space-y-3">
    <p className="text-xs text-muted">更改保留项、跳过或冲突确认后，需要重新计算预览才能执行。</p>
    {!!attention.length && <details open><summary className="mb-2 text-sm">全部冲突 / 换库组（{attention.length}）</summary><div className="max-h-96 space-y-2 overflow-auto">{attention.map(group)}</div></details>}
    <details open><summary className="mb-2 text-sm">全部组（分页）</summary><div className="max-h-96 space-y-2 overflow-auto">{query.data?.items.map(group)}</div></details>
    {query.error && <p role="alert">组列表加载失败：{String(query.error)}</p>}
    <div className="flex items-center gap-3"><Button disabled={!page} onClick={()=>setPage(page-1)}>上一页</Button><span>{page+1} / {Math.max(1,Math.ceil((query.data?.total ?? 0)/20))}</span><Button disabled={!query.data || (page+1)*20>=query.data.total} onClick={()=>setPage(page+1)}>下一页</Button></div>
  </div>;
}
