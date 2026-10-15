import type { CleanupConfig } from "@/lib/maintenance";
import { resourceNames } from "@/lib/maintenance";
import { Button } from "./ui/button";
import { Check, Field, Input, Select } from "./ui/form";
import { Panel } from "./ui/data";
type Options = Omit<CleanupConfig, "query">;
export function MaintenanceOptions({
  cleanup,
  setCleanup,
  busy,
  selectedCount,
  preview,
}: {
  cleanup: Options;
  setCleanup: (value: Options) => void;
  busy: boolean;
  selectedCount: number;
  preview: () => void;
}) {
  return (
    <Panel title="选择清理动作">
      <div className="grid gap-3 py-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="动作">
          <Select
            value={cleanup.action}
            onChange={(e) =>
              setCleanup({
                ...cleanup,
                action: e.target.value as CleanupConfig["action"],
                scope:
                  e.target.value === "resources" ? "library" : cleanup.scope,
                disposition:
                  e.target.value === "remove" ? "trash" : cleanup.disposition,
              })
            }
          >
            <option value="resources">只清理资源文件</option>
            <option value="remove">删除影片副本</option>
            <option value="reconcile">磁盘已删，同步记录</option>
          </Select>
        </Field>
        <Field label="作用范围">
          <Select
            value={cleanup.scope}
            onChange={(e) =>
              setCleanup({
                ...cleanup,
                scope: e.target.value as "library" | "global",
              })
            }
          >
            <option value="library">当前选择的库</option>
            {cleanup.action !== "resources" && (
              <option value="global">所有库中的该版本影片</option>
            )}
          </Select>
        </Field>
        {cleanup.action !== "reconcile" && (
          <Field label="文件处置">
            <Select
              value={cleanup.disposition}
              onChange={(e) =>
                setCleanup({
                  ...cleanup,
                  disposition: e.target.value as "trash" | "permanent",
                })
              }
            >
              <option value="trash">移入回收区</option>
              <option value="permanent">永久删除</option>
            </Select>
          </Field>
        )}
        {cleanup.action !== "reconcile" && cleanup.disposition === "trash" && (
          <Field label="回收保留天数（0 为永久删除）">
            <Input
              type="number"
              min={0}
              max={365}
              value={cleanup.retention_days}
              onChange={(e) =>
                setCleanup({
                  ...cleanup,
                  retention_days: Number(e.target.value),
                })
              }
            />
          </Field>
        )}
        {cleanup.action === "resources" && (
          <Field label="每个 extrafanart 保留前 N 张">
            <Input
              type="number"
              min={0}
              max={10000}
              value={cleanup.keep_extra}
              onChange={(e) =>
                setCleanup({
                  ...cleanup,
                  keep_extra: Number(e.target.value),
                })
              }
            />
          </Field>
        )}
      </div>
      {cleanup.action === "resources" ? (
        <div className="flex flex-wrap gap-4 pb-3">
          {Object.entries(resourceNames).map(([k, label]) => (
            <Check
              key={k}
              checked={cleanup.resource_types.includes(k)}
              onChange={(checked) =>
                setCleanup({
                  ...cleanup,
                  resource_types: checked
                    ? [...cleanup.resource_types, k]
                    : cleanup.resource_types.filter((t) => t !== k),
                })
              }
            >
              {label}
            </Check>
          ))}
        </div>
      ) : (
        <Check
          checked={cleanup.stop_collection}
          onChange={(v) => setCleanup({ ...cleanup, stop_collection: v })}
        >
          完成后停止收录，避免订阅、补源或归库重新添加
        </Check>
      )}
      <div className="space-y-2 py-3">
        <Check
          checked={cleanup.allow_empty}
          onChange={(v) => setCleanup({ ...cleanup, allow_empty: v })}
        >
          已确认主动清空整个目录（仍需已有成功基线和相同根目录身份）
        </Check>
        <p className="text-xs text-muted">
          只清资源保留影片。删除当前库副本会纳入该影片在同库的收件及整理副本；全局操作会展开所有配置库。先生成固定预览，检查影片和完整文件清单后才执行。
        </p>
        <Button
          variant="primary"
          disabled={busy || !selectedCount}
          onClick={preview}
        >
          生成清理预览
        </Button>
      </div>
    </Panel>
  );
}
