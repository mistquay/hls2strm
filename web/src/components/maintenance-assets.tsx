import type { Asset } from "@/lib/maintenance";
import type { Library } from "@/lib/api";
import {
  maintenanceBytes as fmtBytes,
  assetStates,
  nfoStates,
} from "@/lib/maintenance";
import { fmtNum } from "@/lib/format";
import { Button } from "./ui/button";
import { Check } from "./ui/form";
import { Chip, EmptyRow, Panel, Table, Td, Th } from "./ui/data";
interface Props {
  assets: {
    data?: {
      items: Asset[];
      total: number;
      bytes: number;
      extra_bytes: number;
    };
    isFetching: boolean;
  };
  libs: Library[];
  selectedCount: number;
  chosen: (id: number) => boolean;
  toggle: (id: number) => void;
  setBinding: (asset: Asset) => void;
  page: number;
  setPage: (page: number) => void;
  selectAll: () => void;
  clearSelection: () => void;
}
export function MaintenanceAssets({
  assets,
  libs,
  selectedCount,
  chosen,
  toggle,
  setBinding,
  page,
  setPage,
  selectAll,
  clearSelection,
}: Props) {
  return (
    <Panel
      title={`本地副本 · ${fmtNum(assets.data?.total)} 项`}
      actions={
        <>
          <Button size="sm" onClick={selectAll}>
            选择全部筛选结果
          </Button>
          <Button size="sm" onClick={clearSelection}>
            取消选择
          </Button>
        </>
      }
    >
      <p className="py-2 text-sm">
        选中 {fmtNum(selectedCount)} 个副本；筛选占用{" "}
        {fmtBytes(assets.data?.bytes || 0)}，额外剧照{" "}
        {fmtBytes(assets.data?.extra_bytes || 0)}。修改筛选后重新选择。
      </p>
      <Table>
        <thead>
          <tr>
            <Th>
              <Check
                checked={
                  !!assets.data?.items.length &&
                  assets.data.items.every((a) => chosen(a.id))
                }
                onChange={(checked) => {
                  for (const a of assets.data?.items || [])
                    if (chosen(a.id) !== checked) toggle(a.id);
                }}
              >
                当前页
              </Check>
            </Th>
            <Th>影片 / 本地资料</Th>
            <Th>目录与状态</Th>
            <Th>占用</Th>
          </tr>
        </thead>
        <tbody>
          {assets.data?.items.map((a) => (
            <tr key={a.id}>
              <Td>
                <Check checked={chosen(a.id)} onChange={() => toggle(a.id)}>
                  {a.id}
                </Check>
              </Td>
              <Td>
                <b>{a.code || "未关联"}</b>
                {!a.video_id && a.nfo_status === "valid" && (
                  <Button size="sm" onClick={() => setBinding(a)}>
                    关联影片
                  </Button>
                )}
                {a.uncensored ? " · -U" : ""}
                <p>{a.effective.title || a.source_title || "无标题"}</p>
                <p className="text-xs">
                  {a.metadata.release_date || a.metadata.year || "NFO 日期未知"}{" "}
                  · {a.metadata.categories?.join("、") || "NFO 类型未知"}
                </p>
                <details className="mt-1 text-xs">
                  <summary className="cursor-pointer text-accent">
                    资料及来源
                  </summary>
                  <p>演员：{a.metadata.models?.join("、") || "未知"}</p>
                  <p>
                    厂牌：{a.metadata.maker || "未知"}；系列：
                    {a.metadata.series || "未知"}
                  </p>
                  <p className="break-all">NFO：{a.details.nfo_path || "无"}</p>
                  <p>源站标题：{a.source_title || "无"}</p>
                  {a.details.historical_metadata && (
                    <p>
                      历史资料：{a.details.historical_metadata.title} ·{" "}
                      {a.details.historical_metadata.release_date ||
                        a.details.historical_metadata.year}
                      （当前不可用，不作为默认删除依据）
                    </p>
                  )}
                  <p className="text-err">
                    {a.details.nfo_error || a.details.error}
                  </p>
                </details>
              </Td>
              <Td>
                <p className="max-w-lg break-all text-xs">
                  {libs.find((l) => l.id === a.library_id)?.name} · {a.root}/
                  {a.path}
                </p>
                <div className="mt-1 flex flex-wrap gap-2">
                  <Chip>{assetStates[a.status]}</Chip>
                  <Chip>NFO {nfoStates[a.nfo_status]}</Chip>
                  {a.health !== "ok" && (
                    <Chip tone="warn">根目录 {a.health}</Chip>
                  )}
                </div>
              </Td>
              <Td>
                {fmtBytes(a.bytes)}
                <p className="text-xs">
                  额外剧照 {fmtBytes(a.extra_bytes)} / {a.extra_count} 张
                </p>
              </Td>
            </tr>
          ))}
          {!assets.data?.items.length && (
            <EmptyRow cols={4}>
              {assets.isFetching ? "读取中…" : "没有符合条件的已扫描副本"}
            </EmptyRow>
          )}
        </tbody>
      </Table>
      <div className="flex gap-3 py-3">
        <Button disabled={!page} onClick={() => setPage(page - 1)}>
          上一页
        </Button>
        <span>
          {page + 1} / {Math.max(1, Math.ceil((assets.data?.total || 0) / 50))}
        </span>
        <Button
          disabled={(page + 1) * 50 >= (assets.data?.total || 0)}
          onClick={() => setPage(page + 1)}
        >
          下一页
        </Button>
      </div>
    </Panel>
  );
}
