import { DedupePreview, type DedupeGroup } from "./dedupe-preview";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type Job } from "@/lib/api";
import { useRun, useStatus } from "@/lib/queries";
import { navigate } from "@/lib/route";
import { fmtNum } from "@/lib/format";
import { QueryNotice } from "./common";
import { Button } from "./ui/button";
import { Mono, Notice } from "./ui/data";

export interface OperationRequest { kind:"library"|"retirement"|"selection"|"dedupe"; payload:Record<string,unknown> }
interface Preview { attention?:DedupeGroup[]; counts:Record<string,number>; samples:{video_id:number;slug:string;title:string;reason:string;effects:{library_id:number;action:string}[]}[]; warnings:string[]; library_names:Record<string,string>; configuration?:{before:Record<string,unknown>|null;after:Record<string,unknown>} }
interface Receipt { job:Job; preview:Preview|null; preview_token:string|null }
const COUNTS:Record<string,string> = {groups:"重复 / 更正组",selected_groups:"将执行组数",merged_videos:"并入影片",strm_files:"删除 STRM（含画质附件）",retire_dirs:"回收整理目录",missing_outputs:"未找到输出",library_changes:"预计换库组",conflict_groups:"疑似冲突组",edition_corrections:"版本标记更正",matched:"匹配影片",requested:"显式选择",added:"新加入归属",removed:"移出",rewritten:"重写输出",archived:"预计归档输出",kept:"保持现状",unknown:"实际位置待核实",skipped:"不适用",already_exists:"已在输出库",tasks:"预计处理子任务"};
const EFFECTS:Record<string,string> = {add:"加入",remove:"移出",rewrite:"重写",archive:"归档"};

function configValue(key:string,value:unknown,names:Record<string,string>):string {
  if(value==null || value==="" || Array.isArray(value) && !value.length) return "未设置";
  if((key==="sources" || key==="excludes") && Array.isArray(value)) return value.map(id=>names[String(id)] ?? `库 #${id}`).join("、");
  if(key==="versions") return value==="emby" ? "Emby 风格" : "后缀风格";
  if(key==="rule" && typeof value==="object") {
    const rule = value as Record<string,unknown>;
    const labels:Record<string,string> = {categories:"分类",tags:"标签",models:"女优",quality:"画质",keywords:"关键词"};
    return Object.entries(labels).filter(([k])=>Array.isArray(rule[k]) && (rule[k] as unknown[]).length).map(([k,label])=>`${label}：${(rule[k] as string[]).join("、")}`).join(rule.match==="all" ? " 且 " : " 或 ");
  }
  return typeof value==="string" ? value : JSON.stringify(value);
}

export function OperationPreview({request,onApplied,disabled=false,confirmText="确认执行",initialJobId=0,onRequestChange}:{request:OperationRequest;onApplied:(id:number)=>void;disabled?:boolean;confirmText?:string;initialJobId?:number;onRequestChange?:(request:OperationRequest)=>void}) {
  const [jobId,setJobId] = useState(initialJobId);
  const run = useRun();
  const qc = useQueryClient();
  const {data:status} = useStatus();
  const signature = JSON.stringify(request);
  const query = useQuery({queryKey:["operation-preview",jobId],queryFn:({signal})=>api.get<Receipt>(`/api/operations/${jobId}`,signal),enabled:!!jobId,refetchInterval:q=>q.state.data?.job.status==="done" || q.state.data?.job.status==="cancelled" ? false : 1500});
  const tracked = useRef<{id:number;active:boolean}>({id:0,active:false});
  tracked.current = {id:jobId,active:!!jobId && !["done","cancelled"].includes(query.data?.job.status ?? "")};
  const previous = useRef(signature);
  useEffect(()=>{
    if(previous.current!==signature) {
      if(tracked.current.active) void api.post(`/api/jobs/${tracked.current.id}/cancel`).catch(()=>{});
      setJobId(0);
      previous.current=signature;
    }
  },[signature]);
  const compute = async()=>{
    const result = await run(()=>api.post<{job_id:number}>("/api/operations/preview",request),{success:"已创建影响预览任务",invalidate:[["jobs"]]});
    if(result) setJobId(result.job_id);
  };
  const data = query.data;
  const appliedId = Number(data?.job.state.apply_job_id ?? 0);
  const ready = data?.job.status==="done" && !data.job.state.partial_failure && !!data.preview && !!data.preview_token;
  const emptyDedupe = request.kind==="dedupe" && ready && data.preview?.counts.selected_groups===0;
  const emptySelection = request.kind==="selection" && ready && data.preview?.counts.tasks===0;
  const apply = async()=>{
    if(!ready) return;
    const result = await run(()=>api.post<{job_id:number}>(`/api/operations/${jobId}/apply`,{preview_token:data.preview_token}),{success:r=>`已提交执行任务 #${r.job_id}，后台会再次校验范围`,invalidate:[["jobs"],["libraries"],["status"]]});
    if(result) {qc.invalidateQueries({queryKey:["operation-preview",jobId]});onApplied(result.job_id);}
  };
  return <section className="mt-4 space-y-3 rounded-md border border-line p-3" aria-label="操作影响预览">
    <p className="text-sm text-muted">先计算影响，再确认执行。预览只读取本地记录{request.kind==="dedupe" ? "和文件索引" : ""}，不抓取源站、不改归属或文件；执行前会复核范围。</p>
    <div className="flex flex-wrap gap-2">
      {!appliedId && <Button disabled={disabled || run.busy || tracked.current.active} onClick={compute}>{run.busy ? "正在提交…" : jobId ? "重新预览" : "计算影响预览"}</Button>}
      {tracked.current.active && <Button onClick={()=>run(()=>api.post(`/api/jobs/${jobId}/cancel`),{success:"预览已取消",invalidate:[["operation-preview",jobId],["jobs"]]})}>取消预览</Button>}
      {appliedId ? <Button variant="primary" onClick={()=>navigate("jobs",{job:String(appliedId)})}>查看执行任务 #{appliedId}</Button> : <Button variant={(request.kind==="retirement" || request.kind==="dedupe") ? "danger" : "primary"} disabled={!ready || emptySelection || emptyDedupe || disabled || run.busy} onClick={apply}>{confirmText}</Button>}
      {!!jobId && <Button variant="ghost" onClick={()=>navigate("jobs",{job:String(jobId)})}>查看预览任务 #{jobId}</Button>}
    </div>
    {!!jobId && <QueryNotice query={query} label="影响预览" />}
    {tracked.current.active && <p role="status" className="text-sm text-muted">{status?.engine.paused ? "调度已暂停，恢复后继续计算" : data?.job.phase==="queued" ? "预览排队中" : "正在计算影响"} · 已检查 {fmtNum(Number(data?.job.state.checked ?? 0))} 部</p>}
    {data?.job.state.partial_failure && <Notice tone="warn">预览失败，请查看任务中的原因，调整后重新预览。</Notice>}
    {emptyDedupe && <Notice>当前没有将执行的整理组。</Notice>}
    {emptySelection && <Notice>此范围没有待处理的影片，请调整选择或处理方式。</Notice>}
    {!!appliedId && <p className="text-sm text-muted">已确认此预览，处理进度和结果请查看执行任务。</p>}
    {data?.preview && ready && <>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">{Object.entries(data.preview.counts).filter(([k,n])=>n>0 || k==="matched").map(([key,n])=><div key={key} className="rounded bg-panel-2 px-3 py-2 text-sm"><span className="text-muted">{COUNTS[key] ?? key}</span><b className="ml-2">{fmtNum(n)}</b></div>)}</div>
      {data.preview.configuration && <div className="space-y-1 text-xs">{Object.keys(data.preview.configuration.after).filter(k=>configValue(k,data.preview!.configuration!.before?.[k],data.preview!.library_names)!==configValue(k,data.preview!.configuration!.after[k],data.preview!.library_names)).map(k=><p key={k}><b>{({name:"库名称",dir:"库目录",path_template:"路径模板",external_dir:"外部整理目录",versions:"多画质版本",rule:"归库规则",sources:"来源库",excludes:"排除库"} as Record<string,string>)[k] ?? k}</b>：<Mono>{configValue(k,data.preview!.configuration!.before?.[k],data.preview!.library_names)}</Mono> → <Mono>{configValue(k,data.preview!.configuration!.after[k],data.preview!.library_names)}</Mono></p>)}</div>}
      {data.preview.warnings.map(w=><p key={w} className="text-xs text-muted">{w}</p>)}
      {request.kind==="dedupe" && <DedupePreview key={jobId} jobId={jobId} request={request} attention={data.preview.attention ?? []} names={data.preview.library_names} onChange={onRequestChange} disabled={!!appliedId} />}
      {!!data.preview.samples.length && <div className="max-h-60 space-y-2 overflow-auto rounded bg-panel-2 p-2 text-xs">{data.preview.samples.map(sample=><div key={sample.video_id}><b>{sample.slug}</b> {sample.title}<p className="mt-1 text-muted">{sample.reason}{sample.effects.map(effect=>` · ${EFFECTS[effect.action]}「${data.preview!.library_names[effect.library_id] ?? effect.library_id}」`).join("")}</p></div>)}</div>}
    </>}
  </section>;
}
