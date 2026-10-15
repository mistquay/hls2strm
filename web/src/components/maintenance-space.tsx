import type { Library } from "@/lib/api";
import {
  type AssetFilter,
  type MaintenanceJob,
  maintenanceBytes as fmtBytes,
} from "@/lib/maintenance";
import { fmtNum, fmtTime } from "@/lib/format";
import { EmptyRow, Panel, Table, Td, Th } from "./ui/data";
export interface Root {
  library_id: number;
  root: string;
  health: string;
  error: string;
  checked_at: number;
  stats: Record<string, number>;
  disk_free?: number;
  disk_total?: number;
}
interface Overview {
  roots: Root[];
  groups: {
    library_id: number;
    root: string;
    directory: string;
    bytes: number;
    extra_bytes: number;
    assets: number;
  }[];
  archive_root: string;
  jobs: MaintenanceJob[];
}
export function MaintenanceSpace({
  overview,
  query,
  libs,
}: {
  overview: { data?: Overview };
  query: AssetFilter;
  libs: Library[];
}) {
  return (
    <>
      <Panel title="目录空间与健康状态">
        <Table>
          <thead>
            <tr>
              <Th>库 / 根目录</Th>
              <Th>状态</Th>
              <Th>文件逻辑大小</Th>
              <Th>额外剧照</Th>
              <Th>磁盘可用</Th>
              <Th>最近扫描</Th>
            </tr>
          </thead>
          <tbody>
            {overview.data?.roots
              .filter(
                (r) =>
                  !query.library_ids?.length ||
                  query.library_ids.includes(r.library_id),
              )
              .map((r) => (
                <tr key={`${r.library_id}:${r.root}`}>
                  <Td>
                    {libs.find((l) => l.id === r.library_id)?.name}
                    <p className="max-w-xl break-all text-xs">{r.root}</p>
                  </Td>
                  <Td>
                    {r.health === "ok"
                      ? "可用"
                      : r.health === "empty"
                        ? "空目录"
                        : "不可达/未知"}
                    {r.error && <p className="text-err">{r.error}</p>}
                  </Td>
                  <Td>
                    {fmtBytes(r.stats.bytes || 0)}
                    <p className="text-xs">
                      回收区{" "}
                      {r.stats.trash_unavailable
                        ? "不可读取"
                        : fmtBytes(r.stats.trash_bytes || 0)}
                    </p>
                  </Td>
                  <Td>
                    {fmtBytes(r.stats.extra_bytes || 0)} /{" "}
                    {fmtNum(r.stats.extra_count || 0)} 张
                  </Td>
                  <Td>{fmtBytes(r.disk_free)}</Td>
                  <Td>{fmtTime(r.checked_at)}</Td>
                </tr>
              ))}
            {!overview.data?.roots.length && (
              <EmptyRow cols={6}>请先扫描磁盘与 NFO</EmptyRow>
            )}
          </tbody>
        </Table>
        <p className="py-3 text-xs text-muted">
          这里展示最近成功扫描的逻辑大小。硬链接、稀疏文件、快照会影响实际释放量；同盘回收不会立即释放空间。
        </p>
      </Panel>
      <Panel title="一级目录占用（前 100 项）">
        <Table>
          <thead>
            <tr>
              <Th>库 / 目录</Th>
              <Th>占用</Th>
              <Th>额外剧照</Th>
              <Th>副本数</Th>
            </tr>
          </thead>
          <tbody>
            {overview.data?.groups
              .filter(
                (g) =>
                  !query.library_ids?.length ||
                  query.library_ids.includes(g.library_id),
              )
              .map((g) => (
                <tr key={`${g.library_id}:${g.root}:${g.directory}`}>
                  <Td>
                    {libs.find((l) => l.id === g.library_id)?.name} ·{" "}
                    {g.directory}
                  </Td>
                  <Td>{fmtBytes(g.bytes)}</Td>
                  <Td>{fmtBytes(g.extra_bytes)}</Td>
                  <Td>{g.assets}</Td>
                </tr>
              ))}
          </tbody>
        </Table>
        <p className="py-3 text-xs text-muted">
          独立归档区：{overview.data?.archive_root} · 最近扫描{" "}
          {fmtBytes(
            overview.data?.jobs.find(
              (j) => j.kind === "maintenance_scan" && j.status === "done",
            )?.state.archive_bytes || 0,
          )}
          。归档区不作为活跃影片清理范围。
        </p>
      </Panel>
    </>
  );
}
