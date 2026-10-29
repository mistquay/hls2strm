// 几个页面都会用到的操作

import { ask, confirm } from "@/components/confirm";
import { defaultVerifyOptions, verifyParams, VerifyPrompt } from "@/components/verify-options";
import { api, type Library } from "./api";
import type { useRun } from "./queries";
import { navigate } from "./route";

type Run = ReturnType<typeof useRun>;

export async function verifyLibrary(run: Run, lib: Library | null) {
  const name = lib ? `「${lib.name}」` : "全部库";
  let options = { ...defaultVerifyOptions };
  const ok = await ask(
    `核对${name}`,
    <VerifyPrompt onChange={v => { options = v; }} />,
    { confirmText: "开始核对" },
  );
  if (!ok) return;
  await run(() => api.post<{ id: number }>("/api/jobs", { kind: "verify", library_id: lib?.id ?? null, ...verifyParams(options) }), {
    success: r => `已创建核对任务 #${r.id}`,
    invalidate: [["status"], ["jobs"]],
  });
}

export async function rewriteAll(run: Run, lib: Library | null = null) {
  const ok = await ask(
    lib ? `重写「${lib.name}」的输出` : "重写全部输出",
    <div className="space-y-3">
      <p>范围：{lib ? `「${lib.name}」` : "全部输出库"}。用于修改播放地址、令牌或模板后更新文件，不是盘点或手动删除后的记录同步。</p>
      <p>普通库按当前设置重写 STRM/NFO；模板变化可能移动匹配的主图/NFO、删除旧 STRM 和重复目标旁的旧文件。外部库先同步位置，已输出文件通常只原地改 STRM，不重写外部 NFO/图片。</p>
      <p className="text-warn">会对齐并可能删除本服务的多画质版本 STRM；待输出或换库时可能触发已有目录迁移。执行前确认模板及挂载正确，并暂停外部刮削写入。</p>
    </div>,
    { confirmText: "开始重写" },
  );
  if (!ok) return;
  await run(() => api.post<{ id: number }>("/api/jobs", { kind: "rewrite", library_id: lib?.id ?? null }), {
    success: r => `已创建重写任务 #${r.id}`,
    invalidate: [["status"], ["jobs"]],
  });
}

export async function locateLibrary(run: Run, lib: Library) {
  if (!await ask(`同步「${lib.name}」的位置`, <div className="space-y-3">
    <p>在本库收件目录与外部整理目录按 STRM 内容找回已有影片，更新路径记录；不移动主文件、不补丢失的主 STRM、不改 NFO 和图片。</p>
    <p className="text-warn">{lib.versions ? "本库开启了多画质，新位置旁的版本 STRM 可能新增、改写或删除。" : "本库未开启多画质，此次主要更新记录，不搬文件。"} 移到另一库或配置目录之外的文件不会自动转移归属。</p>
  </div>, { confirmText: "开始同步位置" })) return;
  await run(() => api.post<{job_id: number}>(`/api/libraries/${lib.id}/locate`), {
    success: r => `已创建位置同步任务 #${r.job_id}`, invalidate: [["libraries"], ["jobs"]],
  });
}

export async function reclassifyLibrary(run: Run, lib: Library) {
  if (!await ask(`重新计算「${lib.name}」的归属`, <div className="space-y-3">
    <p>重算本库规则，随后执行全局来源/排除归并，可能影响关联库。此操作没有逐文件预览，不是只修改分类标签。</p>
    <p>新增归属会写出文件。移出普通库会删除匹配的 STRM、NFO 和主图；外部库可能删 STRM、将独占资源目录移入回收区，或按设置迁到另一库。</p>
    <p className="text-err">回收保留天数为 0 时可能永久删除。请先核对规则、排除关系和回收设置，暂停外部刮削写入；若只是手动改名，不要用此操作。</p>
  </div>, { danger: true, confirmText: "确认重算并处理文件" })) return;
  const result = await run(() => api.post<{job_id: number}>(`/api/libraries/${lib.id}/reclassify`), { invalidate: [["jobs"], ["libraries"]] });
  if (result) navigate("jobs", {job: String(result.job_id)});
}

export async function previewOrphans(run: Run, lib: Library) {
  if (!await ask(`检查「${lib.name}」的残留目录`, <div className="space-y-3">
    <p>本次只读生成没有主影片、只剩 NFO/图片等资源的目录清单，不删除文件。之后从任务详情查看完整清单再确认处置。</p>
    <p className="text-warn">真正执行时会移动整个残留目录；回收保留 0 天可能永久删除。一天内有变动的目录不处理，仍请在执行前暂停外部刮削。</p>
  </div>, {confirmText: "只读检查并生成清单"})) return;
  const result = await run(() => api.post<{job_id: number}>(`/api/libraries/${lib.id}/tidy?apply=false`), {invalidate: [["jobs"]]});
  if (result) navigate("jobs", {job: String(result.job_id)});
}

export async function deleteLibrary(run: Run, lib: Library) {
  const choice = await confirm({ title: `删除输出库「${lib.name}」`, body: <div className="space-y-3">
    <p>这是删除整个库配置，不是清理几部影片。仅删记录会保留磁盘文件，它们仍可能被 Emby 扫到。</p>
    <p className="text-err">连文件一起删：{lib.external_dir ? "删除外部库的主/版本 STRM，外部 NFO/图片保留。" : "直接删除匹配的 STRM、NFO 和主图，其他资源可能残留。"} 此动作不是可恢复的媒体回收；仍被订阅或其他库引用时会阻止删除。</p>
  </div>, choices: [{value: "keep", label: "只删库记录，保留文件"}, {value: "files", label: "连输出文件一起删除", variant: "danger"}] });
  if (!choice) return;
  await run(() => api.del<{job_id: number}>(`/api/libraries/${lib.id}?delete_files=${choice === "files"}`), {
    success: r => `已创建删库任务 #${r.job_id}`, invalidate: [["libraries"], ["jobs"], ["status"]],
  });
}

export async function runSubscription(run: Run, sub: { id: number; name: string }, mode: "full" | "incremental") {
  if (
    mode === "full" &&
    !(await ask(
      `订阅「${sub.name}」跑一轮全量`,
      "翻完来源的全部页，耗时取决于列表有多大和站点限速，大的列表要几个小时甚至更久（勾了抓详情还要逐部抓）；中途可以暂停或重启，会自动续跑。",
      { confirmText: "开始全量" },
    ))
  )
    return;
  await run(() => api.post<{ job_id: number }>(`/api/subscriptions/${sub.id}/run?mode=${mode}`), {
    success: r => `已创建任务 #${r.job_id}`,
    invalidate: [["status"], ["subscriptions"], ["jobs"]],
  });
}
