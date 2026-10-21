import { fmtBytes } from "./format";
export const maintenanceBytes = (n?: number | null) =>
  n == null ? "-" : n < 1024 ? `${n} B` : fmtBytes(n);
export const maintenanceStatus: Record<string, string> = {
  running: "执行中",
  paused: "已暂停",
  done: "已结束",
  cancelled: "已取消",
  planned: "待处理",
  prepared: "已记录意图",
  trashed: "已回收",
  deleted: "已永久删除",
  absent: "文件已不在",
  restored: "已恢复",
  restoring: "恢复中",
  expired: "回收已过期",
};
export const scanPhases: Record<string, string> = {
  queued: "等待调度",
  waiting_lock: "等待前一个扫描结束",
  preparing: "准备影片索引",
  walking: "扫描文件与 NFO",
  trash: "统计回收区",
  reconciling: "核对磁盘与数据库记录",
  publishing: "发布扫描结果",
  archive: "统计归档空间",
  pruning: "清理旧索引",
  done: "扫描结束",
};
export interface LocalMetadata {
  title?: string;
  code?: string;
  release_date?: string;
  year?: number;
  duration?: number;
  categories?: string[];
  tags?: string[];
  models?: string[];
  maker?: string;
  director?: string;
  series?: string;
  plot?: string;
  errors?: string[];
}
export interface Asset {
  id: number;
  video_id: number | null;
  library_id: number;
  root: string;
  path: string;
  status: string;
  nfo_status: string;
  health: string;
  bytes: number;
  extra_bytes: number;
  extra_count: number;
  code?: string;
  slug?: string;
  uncensored?: number;
  source_title?: string;
  metadata: LocalMetadata;
  effective: LocalMetadata;
  details: {
    nfo_path?: string;
    nfo_error?: string;
    error?: string;
    historical_metadata?: LocalMetadata;
    field_sources?: Record<string, string>;
  };
}
export interface AssetFilter {
  library_ids?: number[];
  video_ids?: number[];
  asset_ids?: number[];
  exclude_ids?: number[];
  source?: "nfo" | "effective";
  q?: string;
  categories?: string[];
  tags?: string[];
  models?: string[];
  category_mode?: "any" | "all" | "not";
  tag_mode?: "any" | "all" | "not";
  maker?: string;
  director?: string;
  series?: string;
  release_from?: string;
  release_to?: string;
  year_from?: number;
  year_to?: number;
  duration_min?: number;
  duration_max?: number;
  bytes_min?: number;
  extra_bytes_min?: number;
  extra_count_min?: number;
  status?: string;
  nfo_status?: string;
  include_unknown?: boolean;
  sort?: string;
  video?: Record<string, unknown>;
}
export interface CleanupConfig {
  query: AssetFilter;
  action: "resources" | "remove" | "reconcile" | "unavailable";
  scope: "library" | "global";
  resource_types: string[];
  keep_extra: number;
  disposition: "trash" | "permanent" | "archive";
  retention_days: number;
  stop_collection: boolean;
  allow_empty: boolean;
}
export interface MaintenanceJob {
  id: number;
  kind: string;
  name: string;
  status: string;
  created_at: number;
  finished_at?: number;
  params: { request?: CleanupConfig; preview_id?: number; restore_of?: number; library_ids?: number[]; force?: boolean };
  operation_request?: CleanupConfig | null;
  state: {
    scan_phase?: string;
    root?: string;
    current_path?: string;
    library_name?: string;
    library_index?: number;
    library_total?: number;
    assets?: number;
    extra_bytes?: number;
    outputs_checked?: number;
    elapsed_seconds?: number;
    updated_at?: number;
    root_errors?: { root: string; error: string }[];
    ready?: boolean;
    preview_token?: string;
    apply_job_id?: number;
    eligible?: number;
    checked?: number;
    files?: number;
    bytes?: number;
    skipped?: number;
    unknown?: number;
    archive_root?: string;
    kept?: number;
    directories?: number;
    error?: string;
    failed?: number;
    failed_roots?: number;
    done?: number;
    trashed_files?: number;
    trashed_bytes?: number;
    deleted_files?: number;
    deleted_bytes?: number;
    space?: Record<
      string,
      { before?: number | null; after?: number | null; total?: number }
    >;
    archive_bytes?: number;
    restored?: number;
    partial_failure?: boolean;
  };
}
export interface MaintenanceItem {
  id: number;
  state: string;
  error: string;
  data: {
    code: string;
    title: string;
    slug: string;
    reason?: string;
    archive_root?: string;
    sources?: { site: string; status: string; retired: boolean }[];
    metadata: LocalMetadata;
    field_sources: Record<string, string>;
    assets: {
      id: number;
      library_id: number;
      root: string;
      path: string;
      nfo_path: string;
    }[];
  };
}
export const resourceNames: Record<string, string> = {
  extra: "额外剧照",
  poster: "海报",
  fanart: "主背景图",
  thumb: "缩略图",
  nfo: "NFO",
  trailer: "预告片",
  subtitle: "字幕",
};
export const assetStates: Record<string, string> = {
  present: "在盘",
  missing: "缺失",
  unmanaged: "未纳管",
  conflict: "冲突",
  cleaned: "已清理",
};
export const nfoStates: Record<string, string> = {
  valid: "有效",
  missing: "缺失",
  invalid: "解析失败",
  conflict: "资料冲突",
};
export const itemStates: Record<string, string> = {
  planned: "待处理",
  running: "执行中",
  done: "已完成",
  skipped: "已跳过",
  failed: "失败",
  restored: "已恢复",
  abandoned: "已放弃",
};
