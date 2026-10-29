import { useState } from "react";
import { ArrowRight, FolderSearch, ShieldAlert } from "lucide-react";
import { useLibraries, useRun } from "@/lib/queries";
import { navigate, replaceParams, useRoute } from "@/lib/route";
import { deleteLibrary, locateLibrary, previewOrphans, reclassifyLibrary, rewriteAll, verifyLibrary } from "@/lib/actions";
import { fileScenarios, libraryOperations, managementParams, operationGroups, type LibraryOperation, type OperationId } from "@/lib/library-management";
import { Button } from "./ui/button";
import { Field, Input, Select } from "./ui/form";
import { Chip, Notice, Panel } from "./ui/data";
import { SiteRetirement } from "./site-retirement";

export function LibraryManagementHome() {
  const route = useRoute();
  const libraries = useLibraries(true);
  const run = useRun();
  const [search, setSearch] = useState("");
  const [scenarioId, setScenario] = useState<string>("moved");
  const [group, setGroup] = useState("all");
  const libs = libraries.data || [];
  const scope = route.params.get("library") || "";
  const lib = libs.find(l => String(l.id) === scope);
  const filteredVideos = !!(route.params.get("ids") || route.params.get("filter"));
  const invalid = !!scope && !lib;
  const multiScope = route.params.has("libraries");
  const scenario = fileScenarios.find(s => s.id === scenarioId)!;
  const filtered = libraryOperations.filter(op => (group === "all" || op.group === group)
    && `${op.title} ${op.alias} ${op.when} ${op.effects}`.toLowerCase().includes(search.trim().toLowerCase()));

  const reason = (op: LibraryOperation) => {
    if (libraries.isPending) return "正在读取库配置";
    if (libraries.error || invalid) return "请先选择有效输出库";
    if (filteredVideos || multiScope) return "请先明确本页库级范围，避免扩大原选择";
    if (lib?.operation_pending) return "本库尚有未完成的配置变更，请先查看原任务";
    if (op.oneLibrary && !lib) return "请先选择一个具体输出库";
    if (op.externalOnly && !lib?.external_dir) return "仅用于配置了外部整理目录的库";
    if (op.id === "delete-library" && lib?.id === 1) return "默认库不能删除";
    if (op.id === "reclassify" && lib && !lib.rule && !lib.sources.length && !lib.excludes.length) return "此库没有规则、来源库或排除关系需要重算";
    return "";
  };
  const go = async (id: OperationId) => {
    const op = libraryOperations.find(o => o.id === id)!;
    if (reason(op)) return;
    if (op.view) { replaceParams(managementParams(route.params, op.view, op.action)); return; }
    if (id === "verify") await verifyLibrary(run, lib || null);
    if (id === "rewrite") await rewriteAll(run, lib || null);
    if (lib && id === "locate") await locateLibrary(run, lib);
    if (lib && id === "reclassify") await reclassifyLibrary(run, lib);
    if (lib && id === "orphans") await previewOrphans(run, lib);
    if (lib && id === "delete-library") await deleteLibrary(run, lib);
  };
  const suggested = libraryOperations.find(o => o.id === (scenario.id === "moved" && lib && !lib.external_dir ? "scan" : scenario.operation))!;
  return <div className="space-y-4">
    <Panel title="先选范围，再选操作" actions={<Button onClick={() => navigate("libraries")}>库配置与订阅</Button>}>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Field label="本页库级操作范围"><Select value={scope} onChange={e => {
          const params = new URLSearchParams(route.params);
          params.delete("libraries");
          if (e.target.value) params.set("library", e.target.value); else params.delete("library");
          replaceParams(params);
        }}><option value="">全部输出库（部分操作需选单库）</option>
          {invalid && <option value={scope}>库 #{scope} 不存在或尚未加载</option>}
          {libs.map(l => <option value={l.id} key={l.id}>{l.name}{l.external_dir ? " · 外部整理" : " · 自管"}</option>)}
        </Select></Field>
        <div className="space-y-2 break-all text-sm">
          <p>{lib ? <><b>{lib.name}</b> · {lib.external_dir ? "外部整理库：收件区只送 STRM，刮削成果在外部目录。" : "自管库：本服务按模板生成 STRM/NFO/主图。"}</> : "盘点、核对和重写可覆盖全部库。同步位置、重新归库、接管及删库必须先选一个具体库。"}</p>
          {lib && <p className="text-xs text-muted">{lib.external_dir ? "收件目录" : "输出目录"}：{lib.root}{lib.external_root && <><br />外部整理目录：{lib.external_root}</>}</p>}
          {!!lib?.operation_pending && <Button size="sm" onClick={() => navigate("jobs", {job: String(lib.operation_job_id)})}>处理未完成配置任务 #{lib.operation_job_id}</Button>}
        </div>
      </div>
      {libraries.error && <p role="alert" className="mt-3 text-err">{libraries.error.message}</p>}
      {(filteredVideos || multiScope) && <Notice tone="warn" actions={<Button onClick={() => {
        const params = new URLSearchParams(route.params); params.delete("ids"); params.delete("filter"); params.delete("libraries"); replaceParams(params);
      }}>清除原筛选，使用当前库级范围</Button>}>当前链接带有影片筛选或多库选择。本页的重写/归库等不是仅处理这些影片，已暂时禁用，避免误扩大范围；可返回“盘点与资料”继续处理原选择范围。</Notice>}
    </Panel>
    <Panel title={<span className="inline-flex items-center gap-2"><FolderSearch className="size-4" />文件变动后，应该用哪个？</span>}>
      <div className="flex flex-wrap gap-2">{fileScenarios.map(s => <Button key={s.id} variant={scenarioId === s.id ? "primary" : "outline"} aria-pressed={scenarioId === s.id} onClick={() => setScenario(s.id)}>{s.title}</Button>)}</div>
      <ol className="my-4 grid list-none gap-3 lg:grid-cols-3">{scenario.steps.map((text, index) => <li key={text} className="flex gap-3 rounded-md border border-line bg-panel-2/40 p-3 text-sm leading-relaxed"><span className="font-mono text-accent">0{index + 1}</span><span>{text}</span></li>)}</ol>
      <div className="flex flex-wrap items-center gap-3"><Button disabled={run.busy || !!reason(suggested)} onClick={() => go(suggested.id)}>{suggested.button}<ArrowRight className="size-4" /></Button><span className="text-xs text-muted">{reason(suggested) || "只进入下一步或打开确认，不会因为选择场景立即删除文件。"}</span></div>
    </Panel>
    <Notice tone="warn"><span className="inline-flex items-center gap-2 font-medium"><ShieldAlert className="size-4 shrink-0" />先区分“删文件”和“删记录”</span><p className="mt-2">手动删盘上的文件不会自动退库，也可能被订阅补回。回收会搬文件，保留 0 天或选择永久删除则无法恢复；归档移到库外但不自动过期。媒体库文件变动后还需 Emby/Jellyfin 自己刷新。</p></Notice>
    <Panel title="操作速查 · 展开查看具体文件影响">
      <div className="mb-4 grid gap-3 sm:grid-cols-2"><Field label="查找操作"><Input value={search} onChange={e => setSearch(e.target.value)} placeholder="例如：重写、纳管、手动删除、回收" /></Field>
        <Field label="操作分组"><Select value={group} onChange={e => setGroup(e.target.value)}><option value="all">全部操作</option>{Object.entries(operationGroups).map(([id, title]) => <option key={id} value={id}>{title}</option>)}</Select></Field></div>
      {Object.entries(operationGroups).map(([key, title]) => {
        const operations = filtered.filter(op => op.group === key);
        return !operations.length ? null : <section className="mb-5 last:mb-0" key={key}>
          <h3 className="mb-3 text-sm font-semibold text-muted">{title}</h3>
          <div className="grid items-start gap-3 xl:grid-cols-2">{operations.map(op => <details key={op.id} className="min-w-0 rounded-lg border border-line p-3" id={`operation-${op.id}`}>
            <summary className="cursor-pointer text-sm"><b>{op.title}</b><span className="mt-1 block text-xs text-muted">对应操作：{op.alias}</span><div className="mt-2 flex flex-wrap gap-1.5">{op.impacts.map(impact => <Chip key={impact} tone={op.danger ? "warn" : "neutral"}>{impact}</Chip>)}</div></summary>
            <div className="mt-3 space-y-3 text-sm leading-relaxed"><p><b>什么时候用：</b>{op.when}</p><p><b>实际影响：</b>{op.effects}</p><p className={op.danger ? "text-warn" : "text-muted"}><b>注意：</b>{op.boundary}</p>
              <Button variant={op.danger ? "outline" : "primary"} disabled={run.busy || !!reason(op)} onClick={() => go(op.id)}>{op.button}</Button>{reason(op) && <p className="text-xs text-muted">{reason(op)}</p>}
            </div>
          </details>)}</div>
        </section>;
      })}
      {!filtered.length && <p className="py-5 text-center text-muted">没有匹配操作，请换个关键词。</p>}
    </Panel>
    <Panel title="其他影响文件的操作与后台行为">
      <div className="space-y-3 text-sm leading-relaxed">
        <p className="text-muted">下面是其他工具的入口，各自选择范围；站点清退和定时任务不受上方单库选择限制。</p>
        <p><b>影片级移出、同片整理、删除源：</b>仍在影片列表按具体影片选择；这些可能删除输出或跨库移动，不等于修改标签。整片清理优先从本中心进入，已有整理任务从任务详情继续。</p>
        <p><b>停用站点与清退站点不同：</b>停用控制抓取/选源，清退可能归档库文件。操作前查看站点清退确认；无可用源归档入口已在本中心。</p>
        <p><b>后台也可能改文件：</b>订阅、归并、核对补回、同步多版本以及到期回收清理不因你离开此页而停止。全局暂停不是全部定时清理的停止开关；手动动文件前同时检查定时任务并暂停外部刮削。</p>
        <div className="flex flex-wrap gap-2"><Button onClick={() => navigate("videos", scope ? {library: scope} : {})}>影片级操作</Button><SiteRetirement /><Button onClick={() => navigate("schedules")}>检查后台定时任务</Button><Button onClick={() => navigate("jobs")}>全部任务与旧版清理记录</Button></div>
      </div>
    </Panel>
  </div>;
}
