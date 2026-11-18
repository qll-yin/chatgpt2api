"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, RefreshCw, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  deleteMailPool,
  fetchMailPool,
  importMailPool,
  releaseStaleMailPool,
  type MailPoolItem,
  type RegisterStatus,
} from "@/lib/register-api";

import { Pagination } from "./pagination";


const STATUS_META: Record<string, { label: string; variant: "secondary" | "outline" | "danger" }> = {
  available: { label: "可用", variant: "secondary" },
  in_use: { label: "使用中", variant: "outline" },
  done: { label: "已完成", variant: "outline" },
  failed: { label: "失败", variant: "danger" },
};

function formatTime(ts?: number | null) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/**
 * 邮箱池。
 *
 * 池化的邮箱来源（outlook / icloud_relay）必须先把邮箱列表导进来，
 * 否则批量注册时一个号都领不到 —— 注册结果页能选 outlook，
 * 但没地方导入，等于选了也用不了。
 */
export function MailPoolPanel() {
  const [items, setItems] = useState<MailPoolItem[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<RegisterStatus | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [status, setStatus] = useState("");
  // ⚠️ Radix 的 Select 不接受空串 value（会抛错并让整棵 React 树崩掉）
  const [kind, setKind] = useState("outlook");
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchMailPool({
        status,
        kind,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setItems(data.items || []);
      setTotal(data.total || 0);
      setStats(data.stats ?? null);
    } catch (error) {
      toast.error(`加载失败：${(error as Error).message ?? error}`);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, status, kind]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleImport = async () => {
    if (!text.trim()) {
      toast.error("请先粘贴邮箱列表");
      return;
    }
    setSaving(true);
    try {
      const result = await importMailPool(text, kind || "outlook");
      toast.success(
        `导入完成：新增 ${result.inserted ?? 0}，更新 ${result.updated ?? 0}，跳过 ${result.skipped ?? 0}`,
      );
      setText("");
      await load();
    } catch (error) {
      toast.error(`导入失败：${(error as Error).message ?? error}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (email: string) => {
    try {
      await deleteMailPool([email]);
      toast.success("已移除");
      await load();
    } catch (error) {
      toast.error(`移除失败：${(error as Error).message ?? error}`);
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">导入邮箱</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">
            池化的邮箱来源（outlook / icloud_relay）要先导进来才能用 ——
            批量注册时从池子里按顺序认领。
            <br />
            格式见下方占位符；<strong>有一行不合法就整批拒绝</strong>，
            不会写进去一半。
          </p>
          <div className="space-y-1.5">
            <Label htmlFor="pool-kind">邮箱类型</Label>
            <Select value={kind} onValueChange={setKind}>
              <SelectTrigger id="pool-kind">
                <SelectValue placeholder="选择要导入的邮箱类型" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="outlook">Outlook（微软邮箱）</SelectItem>
                <SelectItem value="icloud_relay">iCloud 中转</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={5}
            placeholder={"email@outlook.com----password----client_id----refresh_token----relay_url"}
            className="w-full rounded-md border border-stone-200 bg-white p-2 font-mono text-xs outline-none focus:border-stone-400 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-100"
          />
          <div className="flex justify-end">
            <Button onClick={handleImport} disabled={saving}>
              {saving ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Upload className="size-4" />
              )}
              导入
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2">
          <CardTitle className="text-base">
            邮箱池
            {stats ? (
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                可用 {stats.available} / 使用中 {stats.in_use} / 已完成 {stats.done} / 失败{" "}
                {stats.failed}
              </span>
            ) : null}
          </CardTitle>
          <div className="flex items-center gap-2">
            <Select
              // ⚠️ Radix 的 SelectItem 不接受空串 value —— 会抛错并让整棵树崩掉。
              //    「全部状态」用哨兵值表示。
              value={status || "__all__"}
              onValueChange={(v) => {
                setStatus(v === "__all__" ? "" : v);
                setPage(1);
              }}
            >
              <SelectTrigger className="h-8 w-32 text-xs">
                <SelectValue placeholder="全部状态" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">全部状态</SelectItem>
                <SelectItem value="available">可用</SelectItem>
                <SelectItem value="in_use">使用中</SelectItem>
                <SelectItem value="done">已完成</SelectItem>
                <SelectItem value="failed">失败</SelectItem>
              </SelectContent>
            </Select>
            <Button variant="secondary" size="sm" onClick={() => void load()}>
              <RefreshCw className="size-4" />
              刷新
            </Button>
            <Button
              variant="ghost"
              size="sm"
              title="把卡在「使用中」超过 10 分钟的号放回可用（进程崩溃 / 强制停止后用）"
              onClick={() => {
                void (async () => {
                  try {
                    const result = await releaseStaleMailPool(600);
                    toast.success(`已释放 ${result.released} 个卡住的号`);
                    await load();
                  } catch (error) {
                    toast.error(`释放失败：${(error as Error).message ?? error}`);
                  }
                })();
              }}
            >
              释放卡住的号
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>邮箱</TableHead>
                <TableHead className="w-20">类型</TableHead>
                <TableHead className="w-20">状态</TableHead>
                <TableHead className="w-32">导入时间</TableHead>
                <TableHead>最近失败</TableHead>
                <TableHead className="w-20">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-sm text-muted-foreground">
                    {loading ? "加载中…" : "邮箱池是空的，先在上面导入"}
                  </TableCell>
                </TableRow>
              ) : (
                items.map((item) => {
                  const meta = STATUS_META[item.status] ?? {
                    label: item.status,
                    variant: "outline" as const,
                  };
                  return (
                    <TableRow key={item.email}>
                      <TableCell className="max-w-[280px] truncate font-mono text-xs">
                        {item.email}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{item.kind}</TableCell>
                      <TableCell className="whitespace-nowrap">
                        <Badge variant={meta.variant}>{meta.label}</Badge>
                      </TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">
                        {formatTime(item.imported_at)}
                      </TableCell>
                      <TableCell className="max-w-[260px] truncate text-xs text-muted-foreground">
                        {item.fail_reason || "—"}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 whitespace-nowrap px-1.5 text-xs text-red-600 dark:text-red-400"
                          onClick={() => void handleDelete(item.email)}
                        >
                          <Trash2 className="size-3.5" />
                          移除
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })
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
      </Card>
    </div>
  );
}
