import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { useRun } from "@/lib/queries";
import { resourceNames } from "@/lib/maintenance";
import { ask } from "./confirm";
import { Button } from "./ui/button";
import { Panel, Table, Th, Td } from "./ui/data";

export function ResourcePolicies() {
  const [page, setPage] = useState(0);
  const run = useRun();
  const query = useQuery({
    queryKey: ["resource-policies", page],
    queryFn: () =>
      api.get<{
        items: {
          video_id: number;
          library_id: number;
          job_id: number;
          kind: string;
          code: string;
          library_name: string;
        }[];
        total: number;
      }>(`/api/maintenance/resources?offset=${page * 50}`),
  });
  return (
    <Panel title="主动清理的资源（本服务不再自动补回）">
      {query.error && <p className="text-err">{query.error.message}</p>}
      <Table>
        <thead>
          <tr>
            <Th>影片</Th>
            <Th>输出库</Th>
            <Th>资源</Th>
            <Th>操作</Th>
          </tr>
        </thead>
        <tbody>
          {query.data?.items.map((r) => (
            <tr key={`${r.video_id}:${r.library_id}:${r.kind}:${r.job_id}`}>
              <Td>{r.code}</Td>
              <Td>{r.library_name}</Td>
              <Td>
                {resourceNames[r.kind]} · #{r.job_id}
              </Td>
              <Td>
                <Button
                  size="sm"
                  onClick={async () => {
                    if (
                      await ask(
                        "允许重新补回资源",
                        `解除 ${r.code} 的${resourceNames[r.kind]}禁补记录；以后核对或刷新时可能再次生成。`,
                      )
                    )
                      await run(
                        () =>
                          api.del(
                            `/api/maintenance/resources/${r.video_id}/${r.library_id}/${r.kind}/${r.job_id}`,
                          ),
                        { invalidate: [["resource-policies"]] },
                      );
                  }}
                >
                  解除本条禁补
                </Button>
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <div className="flex gap-2 py-3">
        <Button disabled={!page} onClick={() => setPage(page - 1)}>
          上一页
        </Button>
        <Button
          disabled={(page + 1) * 50 >= (query.data?.total || 0)}
          onClick={() => setPage(page + 1)}
        >
          下一页
        </Button>
      </div>
      <p className="pb-3 text-xs text-muted">
        此设置不控制 MDCNG
        的外部写入。多个清理任务有独立记录，需要解除所有相关禁补记录后才会恢复自动补回。
      </p>
    </Panel>
  );
}
