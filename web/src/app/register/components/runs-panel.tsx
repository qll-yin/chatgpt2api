"use client";

import { useCallback, useEffect, useState } from "react";
import { FileText, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchBatchLog, fetchRegisterRuns, type RegisterRunBatch } from "@/lib/register-api";

import { Pagination } from "./pagination";

function formatTime(ts?: number | null) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(
    d.getSeconds(),
  ).padStart(2, "0")}`;
}

/**
 * 运行记录。
 *
 * 按**批次**展示 —— 一次批量注册 = 一行（下面挂多个 run，每个 run 一次注册）。
 * 这样能一眼看到「这一轮跑了几个号、哪些邮箱、日志在哪」。
 */
export function RunsPanel() {
  const [items, setItems] = useState<RegisterRunBatch[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);

  const [logOpen, setLogOpen] = useState(false);
  const [logTitle, setLogTitle] = useState("");
  const [logContent, setLogContent] = useState("");
  const [logLoading, setLogLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchRegisterRuns(pageSize, (page - 1) * pageSize);
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (error) {
      toast.error(`加载失败：${(error as Error).message ?? error}`);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize]);

  useEffect(() => {
    void load();
  }, [load]);

  const openLog = async (batch: RegisterRunBatch) => {
    setLogOpen(true);
    setLogLoading(true);
    setLogTitle(`${batch.batch_id} · 成功 ${batch.ok} / 失败 ${batch.fail}`);
    setLogContent("");
    try {
      const data = await fetchBatchLog(batch.batch_id);
      setLogContent(data.content || "（这一轮没有日志内容）");
    } catch (error) {
      toast.error(`加载日志失败：${(error as Error).message ?? error}`);
      setLogContent(`加载失败：${(error as Error).message ?? error}`);
    } finally {
      setLogLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle className="text-base">
          运行记录
          <span className="ml-2 text-xs font-normal text-muted-foreground">共 {total} 轮</span>
        </CardTitle>
        <Button variant="secondary" size="sm" onClick={() => void load()}>
          <RefreshCw className="size-4" />
          刷新
        </Button>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-28">批次</TableHead>
              <TableHead className="w-32">开始</TableHead>
              <TableHead className="w-24">耗时</TableHead>
              <TableHead className="w-20">成功</TableHead>
              <TableHead className="w-20">失败</TableHead>
              <TableHead>邮箱</TableHead>
              <TableHead className="w-24">日志</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="py-8 text-center text-sm text-muted-foreground">
                  {loading ? "加载中…" : "还没有运行记录"}
                </TableCell>
              </TableRow>
            ) : (
              items.map((batch) => (
                <TableRow key={batch.batch_id}>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {batch.batch_id.slice(0, 10)}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums text-muted-foreground">
                    {formatTime(batch.started_at)}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums text-muted-foreground">
                    {batch.duration != null ? `${batch.duration.toFixed(1)}s` : "—"}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{batch.ok}</Badge>
                  </TableCell>
                  <TableCell>
                    {batch.fail > 0 ? (
                      <Badge variant="danger">{batch.fail}</Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">0</span>
                    )}
                  </TableCell>
                  <TableCell className="max-w-[320px]">
                    {batch.emails.length === 0 ? (
                      <span className="text-xs text-muted-foreground">—</span>
                    ) : (
                      <span className="text-xs" title={batch.emails.join("\n")}>
                        <span className="font-medium">{batch.emails.length} 个</span>
                        <span className="ml-2 text-muted-foreground">
                          {batch.emails.slice(0, 2).join(", ")}
                          {batch.emails.length > 2 ? " …" : ""}
                        </span>
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 whitespace-nowrap px-1.5 text-xs"
                      onClick={() => void openLog(batch)}
                    >
                      <FileText className="size-3.5" />
                      查看
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
        <Pagination
          page={page}
          pageSize={pageSize}
          total={total}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      </CardContent>

      <Dialog open={logOpen} onOpenChange={setLogOpen}>
        <DialogContent className="max-w-4xl">
          <DialogHeader>
            <DialogTitle>运行日志 · {logTitle}</DialogTitle>
            <DialogDescription>这一轮里每个账号的完整日志（按账号分段）</DialogDescription>
          </DialogHeader>
          <pre className="max-h-[60vh] overflow-auto rounded-md bg-stone-950 p-3 font-mono text-xs leading-relaxed text-stone-200">
            {logLoading ? "加载中…" : logContent || "（空）"}
          </pre>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setLogOpen(false)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
