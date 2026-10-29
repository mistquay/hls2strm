// Shared navigation and operation semantics; keep this catalog aligned with the backend.
export const managementTabs = [
  ["center", "操作指南"], ["assets", "盘点与资料"], ["differences", "手动删除后"],
  ["cleanup", "清理文件"], ["unavailable", "无可用源归档"],
  ["metadata", "补全资料"], ["strm", "导入 STRM / 改前缀"],
  ["space", "空间"], ["history", "记录与恢复"],
] as const;
export type ManagementView = typeof managementTabs[number][0];

export function managementView(params: URLSearchParams): ManagementView {
  const tab = params.get("tab");
  if (managementTabs.some(([key]) => key === tab)) return tab as ManagementView;
  if (params.has("job")) return "history";
  if (params.has("ids") || params.has("filter")) return "assets";
  return "center";
}

export function managementParams(params: URLSearchParams, view: ManagementView, action?: string) {
  const next = new URLSearchParams(params);
  next.set("tab", view);
  next.delete("job");
  next.delete("action");
  if (action) next.set("action", action);
  return next;
}

export function canonicalRoute(page: string, params: URLSearchParams) {
  return page === "strm" ? { page: "maintenance", params: managementParams(params, "strm") } : { page, params };
}

export function cleanupDefaults(view: ManagementView, action: string | null) {
  const selected = view === "differences" ? "reconcile" : action === "remove" ? "remove" : "resources";
  return { action: selected, disposition: selected === "resources" ? "permanent" : "trash" } as const;
}

export function managementLibraryIds(params: URLSearchParams): number[] {
  const raw = params.has("libraries") ? params.get("libraries") : params.get("library");
  return [...new Set((raw || "").split(",").filter(Boolean).map(Number).filter(id => Number.isSafeInteger(id) && id > 0))];
}

export const operationGroups = { inspect: "盘点与文件变更", organize: "输出与归属", clean: "清理与恢复" } as const;
export type OperationId = "scan" | "locate" | "verify" | "reconcile" | "rewrite" | "reclassify" | "adopt" | "prefix" | "metadata" | "resources" | "remove" | "orphans" | "archive" | "history" | "delete-library";
export interface LibraryOperation {
  id: OperationId; group: keyof typeof operationGroups; title: string; alias: string;
  when: string; impacts: string[]; effects: string; boundary: string;
  button: string; view?: ManagementView; action?: string; oneLibrary?: boolean; externalOnly?: boolean; danger?: boolean;
}
export const libraryOperations: LibraryOperation[] = [
  { id: "scan", group: "inspect", title: "盘点磁盘与资料", alias: "扫描磁盘与 NFO / 强制重读",
    when: "先看看磁盘实际有什么，哪些缺失、NFO 有什么资料。", impacts: ["不改媒体文件", "更新索引与路径记录"],
    effects: "读取 STRM、NFO 和文件信息，成功后更新维护索引及可识别的已有输出路径。强制重读只是不复用 NFO 缓存。",
    boundary: "外部库只盘点外部整理目录，不自动纳管新影片、不补文件、不刷新 Emby。", button: "进入盘点", view: "assets" },
  { id: "locate", group: "inspect", title: "找回改名 / 移动后的路径", alias: "同步位置", oneLibrary: true, externalOnly: true,
    when: "外部工具或你手动在本库目录范围内移动、改名了 STRM。", impacts: ["更新路径记录", "可能增删版本 STRM"],
    effects: "按 STRM 内容扫描本库收件目录和外部目录，更新已有输出的位置。主 STRM 不搬、不补，NFO 和图片不改。",
    boundary: "开启多画质时，新位置旁的本服务版本 STRM 可能新增、改写或删除。不会跨库找文件，也不会自动纳管新影片。", button: "确认同步位置" },
  { id: "verify", group: "inspect", title: "检查输出 / 补回误删文件", alias: "核对并补回", when: "怀疑输出缺失，或希望重新生成误删的 STRM。",
    impacts: ["可选写文件", "可选联网", "可能增删版本 STRM"],
    effects: "默认不补回。显式开启后，普通库按设置补 STRM/NFO/封面；外部库找不到 STRM 时可写回收件区，交给刮削器重整。",
    boundary: "核对仍会同步外部库位置，可能对齐版本 STRM。外部 NFO/图片不由核对补回；主动删除且不想恢复时不要选补回。", button: "检查选项与影响" },
  { id: "reconcile", group: "inspect", title: "文件已删，不想再补回来", alias: "磁盘已删，同步记录", when: "你已经手动删除影片，想清掉失效关联并阻止以后重新收录。",
    impacts: ["清理失效记录", "可停止收录"], effects: "先盘点、确认确实缺失，再按固定预览移除失效输出关联。此动作本身不删除残留 NFO 或图片。",
    boundary: "挂载离线不能当删除；同库仍有副本时不能按缺失退库。保留“停止收录”才能阻止该范围再次输出。", button: "处理手动删除", view: "differences" },
  { id: "rewrite", group: "organize", title: "把当前设置写入已有输出", alias: "重写输出", when: "修改了对外地址、播放令牌、播放模式或输出模板。",
    impacts: ["改写 STRM / NFO", "可能移动 / 删除旧输出"],
    effects: "普通库按模板重写 STRM，按设置重写 NFO；模板变化会迁移匹配的资源并删除旧 STRM/重复目标旁的旧文件。外部库已定位文件通常只原地改 STRM。",
    boundary: "会对齐并可能删除本服务版本 STRM；待输出或换库可能走已有迁移逻辑。不是只读检查，也不是根据磁盘自动退库。", button: "确认重写影响" },
  { id: "reclassify", group: "organize", title: "按规则重新计算影片归属", alias: "重新归库", oneLibrary: true, danger: true,
    when: "修改了规则、来源库或排除库关系，希望重算归属。", impacts: ["新增输出", "可能移库 / 删除 / 回收"],
    effects: "当前库规则重新求值，之后执行全局来源/排除归并。普通库移出时删除匹配的 STRM/NFO/主图；外部库删 STRM，独占资源目录按设置迁移或回收。",
    boundary: "会影响关联库，不是给文件改分类标签。回收保留天数为 0 时可能直接删除；此操作没有逐文件预览。", button: "确认归库影响" },
  { id: "adopt", group: "organize", title: "把已有 STRM 纳入管理", alias: "纳管", when: "磁盘上有 STRM，但尚未与本服务输出记录关联。",
    impacts: ["建立关联", "改写地址", "普通库可能搬文件"],
    effects: "先扫描 STRM，再指定纳管目标。普通库按模板归位，原文件可能移走或删除；外部库通常原地更新 STRM。缺资料时可选联网。",
    boundary: "不是仅登记路径。文件名识别可能误认其他片源；缺影片时目前走 Jable 详情获取，不是通用媒体导入器。", button: "扫描并检查纳管范围", view: "strm" },
  { id: "prefix", group: "organize", title: "替换已有 STRM 的地址前缀", alias: "批量改前缀 / 回滚", when: "只换地址前缀，不希望重排目录或重写 NFO。",
    impacts: ["改 STRM 内容"], effects: "基于 STRM 扫描结果预览替换；原路径写回 STRM，保存变更记录供回滚。",
    boundary: "不移动文件、不整理库归属；回滚时文件变化会跳过。与纳管共用扫描入口。", button: "扫描并预览改前缀", view: "strm" },
  { id: "metadata", group: "organize", title: "用已有源站资料补缺", alias: "源站资料兜底补全", oneLibrary: true, externalOnly: true,
    when: "外部刮削器缺 NFO/图片、只刮到部分字段，或有未整理文件。", impacts: ["补 NFO / 图片", "接管模式会移动"],
    effects: "三个模式分别预览：补缺失文件、已有 NFO 补空字段、接管到外部目录 /_hls补全/。前两种不搬影片，接管核验后移除原文件。",
    boundary: "保留已有资料，接管前需暂停外部刮削。不是重抓所有详情，也不是清理无可用源。", button: "选择补全模式", view: "metadata" },
  { id: "resources", group: "clean", title: "只删图片、NFO 等资源", alias: "只清理资源文件", danger: true,
    when: "想精简 extrafanart，或明确删除指定类型的资源，但保留影片。", impacts: ["回收或永久删除", "影片仍在库中"],
    effects: "只处理预览选定的资源类型；额外剧照可保留前 N 张。不会删除主 STRM，不会把影片退库。默认额外图片处置是永久删除。",
    boundary: "确认前核对处置方式；本服务记住资源禁补，外部刮削器不受此限制。", button: "选择资源并预览", view: "cleanup", action: "resources" },
  { id: "remove", group: "clean", title: "删除影片副本并停止收录", alias: "删除影片副本", danger: true,
    when: "影片文件还在，决定不再保留这个库或所有库中的该版本。", impacts: ["整组文件回收 / 删除", "移除输出关联"],
    effects: "按确认的副本范围处理 STRM、NFO、图片、字幕等已识别文件，默认回收；可选择永久删除。默认禁止所选范围重新收录。",
    boundary: "全局范围会展开所有库。回收 0 天等于永久删除，永久删除无法恢复；有共享目录等冲突时跳过。", button: "选择影片并预览", view: "cleanup", action: "remove" },
  { id: "orphans", group: "clean", title: "清掉没有影片的残留目录", alias: "清理残留目录", oneLibrary: true, externalOnly: true, danger: true,
    when: "外部整理目录只剩 NFO/图片，主 STRM 已经不在。", impacts: ["移动整个残留目录", "0 天可能永久删除"],
    effects: "先只读生成清单，再从任务详情确认。仅处理符合原有保护条件、近期没有变动的残留目录，移入该库回收区。",
    boundary: "不是删库内仍有 STRM 的影片。不要用它清理外部刮削进行中的目录；按现有回收保留天数执行。", button: "生成残留检查清单" },
  { id: "archive", group: "clean", title: "把无可用源影片移出媒体库", alias: "无可用源 / 清退残留归档", danger: true,
    when: "源已删除、停用或无支持线路，想把对应文件移到库外保存。", impacts: ["移到库外归档", "停止所选库收录"],
    effects: "直接核查外部目录和收件目录，确认后归档独占目录的 STRM、NFO、图片等。归档不自动过期，不等于释放空间。",
    boundary: "只判断本地源策略，不实际联网验证能否播放；归档后可从原记录预览恢复。", button: "核查并预览归档", view: "unavailable" },
  { id: "history", group: "clean", title: "恢复文件 / 解除禁收禁补", alias: "记录与恢复", when: "要找回本服务回收或归档的文件，或允许再次收录。",
    impacts: ["恢复会移动文件", "解除规则只改记录"], effects: "从原清理任务预览恢复；路径有冲突不覆盖。解除禁收/禁补只允许以后再收录或补写，不自动找回文件。",
    boundary: "永久删除、已过期回收及未记录的手动删除无法恢复。STRM 前缀回滚和旧清理任务在各自任务详情中。", button: "查看记录与恢复", view: "history" },
  { id: "delete-library", group: "clean", title: "删除整个输出库配置", alias: "删除输出库", oneLibrary: true, danger: true,
    when: "不再使用这个输出库，而不是只清几部影片。", impacts: ["删除库记录", "可选直接删除输出文件"],
    effects: "可只删记录、保留文件；若同时删文件，普通库删除匹配的 STRM/NFO/主图，外部库删除主/版本 STRM，但保留外部 NFO/图片。",
    boundary: "不是可恢复的整片回收。库仍被订阅或其他库引用时不能删除；保留的文件仍可能被 Emby 扫到。", button: "检查删除库选项" },
];

export const fileScenarios = [
  { id: "moved", title: "我改名 / 移动了文件", steps: ["先确认文件仍在同一库的收件或外部目录内，且 STRM 内容没变。", "外部库用同步位置；要完全不改磁盘可先盘点（只扫描外部目录）。普通库先盘点，不要用重写来保留手动命名。", "移到另一库或库外不会自动转移归属；先核对目标库、用纳管/应用内移库，再处理旧关联。不要把找不到等同已删除。"], operation: "locate" },
  { id: "deleted", title: "我手动删了，不想补回", steps: ["先确认挂载正常；可暂停本服务调度及外部刮削，防止处理中又生成文件。", "盘点后在“手动删除后”选择“磁盘已删，同步记录”，保留“停止收录”，核对固定预览再执行。", "经常手动删文件时，先建立成功扫描基线并启用“缺失先等待确认”。只删图片/NFO 不等于整片退库。"], operation: "reconcile" },
  { id: "restore", title: "我误删了，想找回来", steps: ["本服务回收或归档过的，先到“记录与恢复”找原任务。", "手动删掉的 STRM 可按数据库记录补回，但这不是恢复原刮削成果。", "外部 NFO/图片先查备份；无备份时可尝试资料补全，不能保证原图、剧情和评分恢复。"], operation: "history" },
  { id: "address", title: "我改了播放地址 / 令牌", steps: ["只换已有 STRM 地址前缀，用“导入 STRM / 改前缀”先预览。", "需要按当前播放设置更新全部输出时，使用重写；先核对模板是否也变了。", "普通库可能重写 NFO、移动文件；外部库已输出文件主要原地改 STRM。不要为了地址变更使用重新归库。"], operation: "rewrite" },
  { id: "import", title: "磁盘有文件，系统没管理", steps: ["先扫描 STRM，区分本服务地址、已知 CDN 和仅按文件名识别。", "确认影片与目标库后再纳管；它可能改播放地址、搬文件，不是单纯登记。", "任意本地视频、115/Alist 同番号片源不能直接当成本服务源接管；只有 NFO 的关联不等于纳管 STRM。"], operation: "adopt" },
] as const;
