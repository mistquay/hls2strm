import { type Subscription } from "@/lib/api";
import { fmtScheduledTime } from "@/lib/format";

export function nextSubscriptionTime(sub: Subscription): string {
  if (!sub.enabled) return "已停用";
  if (!sub.cron && (sub.cron !== null || !sub.interval)) return "仅手动";
  if (!sub.initialized) return "等待首轮全量";
  return sub.next_run_at ? fmtScheduledTime(sub.next_run_at, sub.timezone) : "—";
}
