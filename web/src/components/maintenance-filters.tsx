import { assetStates, nfoStates, type AssetFilter } from "@/lib/maintenance";
import { Button } from "./ui/button";
import { Check, Field, Input, Select } from "./ui/form";
import { Panel } from "./ui/data";
const words = (text: string) => text.split(/[,，]/).map((v) => v.trim());
const num = (text: string, scale = 1) =>
  text === "" ? undefined : Number(text) * scale;
export function MaintenanceFilters({
  query,
  update,
}: {
  query: AssetFilter;
  update: (patch: Partial<AssetFilter>) => void;
}) {
  return (
    <Panel title="筛选条件">
      <div className="grid gap-3 py-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="资料来源">
          <Select
            value={query.source}
            onChange={(e) =>
              update({ source: e.target.value as "nfo" | "effective" })
            }
          >
            <option value="nfo">可信 NFO 字段</option>
            <option value="effective">NFO 优先，源站补缺</option>
          </Select>
        </Field>
        <Field label="番号、标题或目录">
          <Input
            value={query.q || ""}
            onChange={(e) => update({ q: e.target.value })}
          />
        </Field>
        <Field label="影片类型（逗号分隔）">
          <Input
            value={query.categories?.join(",") || ""}
            onChange={(e) => update({ categories: words(e.target.value) })}
          />
        </Field>
        <Field label="类型匹配">
          <Select
            value={query.category_mode || "any"}
            onChange={(e) =>
              update({
                category_mode: e.target.value as "any" | "all" | "not",
              })
            }
          >
            <option value="any">包含任一</option>
            <option value="all">包含全部</option>
            <option value="not">不包含任一</option>
          </Select>
        </Field>
        <Field label="发行日期从（含当天）">
          <Input
            type="date"
            value={query.release_from || ""}
            onInput={(e) => update({ release_from: e.currentTarget.value })}
            onChange={(e) => update({ release_from: e.target.value })}
          />
        </Field>
        <Field label="发行日期至（含当天）">
          <Input
            type="date"
            value={query.release_to || ""}
            onInput={(e) => update({ release_to: e.currentTarget.value })}
            onChange={(e) => update({ release_to: e.target.value })}
          />
        </Field>
        <Field label="发行年份从">
          <Input
            type="number"
            min={1000}
            max={9999}
            value={query.year_from ?? ""}
            onChange={(e) => update({ year_from: num(e.target.value) })}
          />
        </Field>
        <Field label="发行年份至">
          <Input
            type="number"
            min={1000}
            max={9999}
            value={query.year_to ?? ""}
            onChange={(e) => update({ year_to: num(e.target.value) })}
          />
        </Field>
        <Field label="磁盘状态">
          <Select
            value={query.status || ""}
            onChange={(e) => update({ status: e.target.value })}
          >
            <option value="">全部状态</option>
            {Object.entries(assetStates).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="NFO 状态">
          <Select
            value={query.nfo_status || ""}
            onChange={(e) => update({ nfo_status: e.target.value })}
          >
            <option value="">全部状态</option>
            {Object.entries(nfoStates).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="额外剧照至少（MiB）">
          <Input
            type="number"
            min={0}
            value={
              query.extra_bytes_min === undefined
                ? ""
                : query.extra_bytes_min / 1024 ** 2
            }
            onChange={(e) =>
              update({ extra_bytes_min: num(e.target.value, 1024 ** 2) })
            }
          />
        </Field>
        <Field label="排序">
          <Select
            value={query.sort}
            onChange={(e) => update({ sort: e.target.value })}
          >
            <option value="bytes">目录大小</option>
            <option value="extra_bytes">额外剧照占用</option>
            <option value="release">发行日期</option>
            <option value="id">扫描顺序</option>
          </Select>
        </Field>
      </div>
      <details className="pb-3">
        <summary className="cursor-pointer text-sm">更多条件</summary>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="标签（逗号分隔）">
            <Input
              value={query.tags?.join(",") || ""}
              onChange={(e) => update({ tags: words(e.target.value) })}
            />
          </Field>
          <Field label="标签匹配">
            <Select
              value={query.tag_mode || "any"}
              onChange={(e) =>
                update({
                  tag_mode: e.target.value as "any" | "all" | "not",
                })
              }
            >
              <option value="any">包含任一</option>
              <option value="all">包含全部</option>
              <option value="not">不包含任一</option>
            </Select>
          </Field>
          <Field label="演员（逗号分隔）">
            <Input
              value={query.models?.join(",") || ""}
              onChange={(e) => update({ models: words(e.target.value) })}
            />
          </Field>
          {(["maker", "director", "series"] as const).map((key, index) => (
            <Field key={key} label={["厂牌", "导演", "系列"][index]}>
              <Input
                value={query[key] || ""}
                onChange={(e) => update({ [key]: e.target.value })}
              />
            </Field>
          ))}
          <Field label="时长至少（分钟）">
            <Input
              type="number"
              min={0}
              value={
                query.duration_min === undefined ? "" : query.duration_min / 60
              }
              onChange={(e) =>
                update({ duration_min: num(e.target.value, 60) })
              }
            />
          </Field>
          <Field label="时长至多（分钟）">
            <Input
              type="number"
              min={0}
              value={
                query.duration_max === undefined ? "" : query.duration_max / 60
              }
              onChange={(e) =>
                update({ duration_max: num(e.target.value, 60) })
              }
            />
          </Field>
          <Field label="目录至少（MiB）">
            <Input
              type="number"
              min={0}
              value={
                query.bytes_min === undefined ? "" : query.bytes_min / 1024 ** 2
              }
              onChange={(e) =>
                update({ bytes_min: num(e.target.value, 1024 ** 2) })
              }
            />
          </Field>
          <Field label="额外剧照至少（张）">
            <Input
              type="number"
              min={0}
              value={query.extra_count_min ?? ""}
              onChange={(e) => update({ extra_count_min: num(e.target.value) })}
            />
          </Field>
          <Field label="版本">
            <Select
              value={
                query.video?.uncensored === true
                  ? "u"
                  : query.video?.uncensored === false
                    ? "normal"
                    : ""
              }
              onChange={(e) =>
                update({
                  video: {
                    ...query.video,
                    uncensored:
                      e.target.value === "" ? null : e.target.value === "u",
                  },
                })
              }
            >
              <option value="">不限</option>
              <option value="normal">普通版 / 原生无码</option>
              <option value="u">-U 版</option>
            </Select>
          </Field>
          <Check
            checked={query.include_unknown || false}
            onChange={(v) => update({ include_unknown: v })}
          >
            显式包含未知字段
          </Check>
        </div>
      </details>
      {!!query.video_ids?.length && (
        <p className="pb-2 text-sm">
          限定 {query.video_ids.length} 部从影片页选入的影片{" "}
          <Button size="sm" onClick={() => update({ video_ids: [] })}>
            解除限定
          </Button>
        </p>
      )}
    </Panel>
  );
}
