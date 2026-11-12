"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchRegisterRuns, type RegisterRun } from "@/lib/register-api";

import { Pagination } from "./pagination";

const PAGE_SIZE = 20;

const categoryLabel: Record<string, string> = {
  network: "网络",
  account: "账号",
  cf_blocked: "CF拦截",
  mail_limited: "邮箱受限",
  unknown: "未知",
};

function formatTime(ts?: number | null) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(
    d.getSeconds(),
  ).padStart(2, "0")}`;
}

export function RunsPanel() {
  const [items, setItems] = useState<RegisterRun[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchRegisterRuns(PAGE_SIZE, (page - 1) * PAGE_SIZE);
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (error) {
      toast.error(`加载失败：${(error as Error).message ?? error}`);
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    void load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle className="text-base">运行记录</CardTitle>
        <Button variant="secondary" size="sm" onClick={() => void load()}>
          <RefreshCw className="size-4" />
          刷新
        </Button>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>状态</TableHead>
              <TableHead>邮箱</TableHead>
              <TableHead>分类</TableHead>
              <TableHead>开始</TableHead>
              <TableHead>耗时</TableHead>
              <TableHead>错误</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-sm text-muted-foreground">
                  {loading ? "加载中…" : "还没有运行记录"}
                </TableCell>
              </TableRow>
            ) : (
              items.map((run) => (
                <TableRow key={run.run_id}>
                  <TableCell>
                    <Badge
                      variant={
                        run.status === "done"
                          ? "secondary"
                          : run.status === "failed"
                            ? "danger"
                            : "outline"
                      }
                    >
                      {run.status === "done"
                        ? "成功"
                        : run.status === "failed"
                          ? "失败"
                          : "运行中"}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-medium">{run.email || "—"}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {run.error_category
                      ? categoryLabel[run.error_category] ?? run.error_category
                      : "—"}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums text-muted-foreground">
                    {formatTime(run.started_at)}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums text-muted-foreground">
                    {run.duration ? `${run.duration.toFixed(1)}s` : "—"}
                  </TableCell>
                  <TableCell className="max-w-[280px] truncate text-xs text-muted-foreground">
                    {run.error || "—"}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
        <Pagination
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onPageChange={setPage}
        />
      </CardContent>
    </Card>
  );
}
