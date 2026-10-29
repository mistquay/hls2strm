import { useState } from "react";
import { Check } from "./ui/form";

export const defaultVerifyOptions = { repair: false, covers: true, details: false, force_external: false };
export type VerifyValues = typeof defaultVerifyOptions;

export function verifyParams(v: VerifyValues) {
  return { repair: v.repair, covers: v.repair && v.covers, detail: v.repair && v.details, force_external: v.repair && v.force_external };
}

export const verifyHint = "默认不补回主文件。普通库检查 STRM 地址、NFO 和封面；外部库主要检查 STRM，仍会同步位置，开启多画质时可能增删本服务版本 STRM，所以不是完全只读。显式勾选补回后，普通库可重写 STRM/NFO、补图或抓详情；外部库两边都找不到时按策略写回收件区，不补外部 NFO/图片。主动删除且不想恢复时，请用“手动删除后 → 磁盘已删，同步记录”。挂载异常不要强制补回。";

export function VerifyOptions({ value, onChange }: { value: VerifyValues; onChange: (v: VerifyValues) => void }) {
  const set = (patch: Partial<VerifyValues>) => onChange({ ...value, ...patch });
  return (
    <div className="flex min-h-8 flex-wrap items-center gap-x-4 gap-y-2">
      <Check checked={value.repair} onChange={repair => set({ repair })}>发现问题就补回</Check>
      <Check checked={value.covers} disabled={!value.repair} onChange={covers => set({ covers })}>补封面（要下载）</Check>
      <Check checked={value.details} disabled={!value.repair} onChange={details => set({ details })}>没详情的抓详情（要联网）</Check>
      <Check checked={value.force_external} disabled={!value.repair} onChange={force_external => set({ force_external })}>外部整理目录不在或是空的也写回</Check>
    </div>
  );
}

export function VerifyPrompt({ onChange }: { onChange: (v: VerifyValues) => void }) {
  const [value, setValue] = useState(defaultVerifyOptions);
  return <div className="space-y-4"><p>{verifyHint}</p><VerifyOptions value={value} onChange={v => { setValue(v); onChange(v); }} /></div>;
}
