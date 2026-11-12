"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Copy, Download, Eye, LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
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
import {
  fetchRegistered,
  fetchRegisteredDetail,
  importRegisteredToPool,
  markRegisteredAbnormal,
  type RegisteredItem,
} from "@/lib/register-api";

import { Pagination } from "./pagination";


/**
 * 注册结果：勾选账号 → 批量导入号池。
 *
 * 两种导入方式：
 *   1. 勾选下面的账号 → 「导入号池」（按 access_token----proxy 格式写入）
 *   2. 粘贴文本 → 「按文本导入」（格式：access_token----proxy，每行一个）
 */
export function RegisteredPanel() {
  const [items, setItems] = useState<RegisteredItem[]>([]);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [withProxy, setWithProxy] = useState(true);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [marking, setMarking] = useState(false);
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof fetchRegisteredDetail>>["item"] | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchRegistered(pageSize, (page - 1) * pageSize);
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

  const allSelected = items.length > 0 && items.every((i) => selected.has(i.email));
  const selectedCount = selected.size;

  const toggleAll = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allSelected) {
        items.forEach((i) => next.delete(i.email));
      } else {
        items.forEach((i) => next.add(i.email));
      }
      return next;
    });
  };

  const toggleOne = (email: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(email)) next.delete(email);
      else next.add(email);
      return next;
    });
  };

  const handleImportSelected = async () => {
    if (selectedCount === 0) {
      toast.error("请先勾选要导入的账号");
      return;
    }
    setImporting(true);
    try {
      const result = await importRegisteredToPool([...selected], withProxy);
      toast.success(
        `导入完成：新增 ${result.imported} 个，跳过 ${result.skipped} 个` +
          (result.missing.length ? `，${result.missing.length} 个无凭证` : ""),
      );
      setSelected(new Set());
      await load();
    } catch (error) {
      toast.error(`导入失败：${(error as Error).message ?? error}`);
    } finally {
      setImporting(false);
    }
  };

  const handleMarkAbnormal = async (abnormal: boolean) => {
    if (selectedCount === 0) {
      toast.error("请先勾选要标注的账号");
      return;
    }
    setMarking(true);
    try {
      const result = await markRegisteredAbnormal([...selected], abnormal);
      toast.success(
        `已标注 ${result.updated} 个账号` +
          (abnormal ? "，并释放了它们占用的代理配额" : ""),
      );
      setSelected(new Set());
      await load();
    } catch (error) {
      toast.error(`标注失败：${(error as Error).message ?? error}`);
    } finally {
      setMarking(false);
    }
  };

  const handleShowDetail = async (email: string) => {
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    try {
      const data = await fetchRegisteredDetail(email);
      setDetail(data.item);
    } catch (error) {
      toast.error(`加载详情失败：${(error as Error).message ?? error}`);
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2">
          <CardTitle className="text-base">
            注册结果
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              共 {total} 个
            </span>
          </CardTitle>
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <Checkbox
                checked={withProxy}
                onCheckedChange={(v) => setWithProxy(Boolean(v))}
              />
              携带代理
            </label>
            <Button variant="secondary" size="sm" onClick={() => void load()}>
              <RefreshCw className="size-4" />
              刷新
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void handleMarkAbnormal(true)}
              disabled={marking || selectedCount === 0}
              title="标注异常会释放该号占用的代理配额"
            >
              {marking ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <AlertTriangle className="size-4" />
              )}
              标注异常（{selectedCount}）
            </Button>
            <Button size="sm" onClick={handleImportSelected} disabled={importing || selectedCount === 0}>
              {importing ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Download className="size-4" />
              )}
              导入号池（{selectedCount}）
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <Checkbox checked={allSelected} onCheckedChange={toggleAll} />
                </TableHead>
                <TableHead className="w-20">状态</TableHead>
                <TableHead>邮箱</TableHead>
                <TableHead className="w-24">access_token</TableHead>
                <TableHead className="w-24">refresh_token</TableHead>
                <TableHead>代理</TableHead>
                <TableHead className="w-20">详情</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-sm text-muted-foreground">
                    {loading ? "加载中…" : "还没有注册成功的账号"}
                  </TableCell>
                </TableRow>
              ) : (
                items.map((item) => (
                  <TableRow key={item.email}>
                    <TableCell>
                      <Checkbox
                        checked={selected.has(item.email)}
                        onCheckedChange={() => toggleOne(item.email)}
                      />
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      {item.abnormal ? (
                        <Badge variant="danger">异常</Badge>
                      ) : (
                        <Badge variant="outline">正常</Badge>
                      )}
                    </TableCell>
                    <TableCell className="font-medium">{item.email}</TableCell>
                    <TableCell>
                      {item.has_access_token ? (
                        <Badge variant="secondary">有</Badge>
                      ) : (
                        <Badge variant="outline">无</Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      {item.has_refresh_token ? (
                        <Badge variant="secondary">有</Badge>
                      ) : (
                        <Badge variant="outline">无</Badge>
                      )}
                    </TableCell>
                    <TableCell className="max-w-[240px] truncate text-xs text-muted-foreground">
                      {item.proxy || "—"}
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 whitespace-nowrap px-1.5 text-xs"
                        onClick={() => void handleShowDetail(item.email)}
                      >
                        <Eye className="size-3.5" />
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
      </Card>

      {/* 账户详情 */}
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>账户详情</DialogTitle>
            <DialogDescription>
              {detail?.email || (detailLoading ? "加载中…" : "")}
            </DialogDescription>
          </DialogHeader>
          {detailLoading ? (
            <p className="py-8 text-center text-sm text-muted-foreground">加载中…</p>
          ) : detail ? (
            <div className="max-h-[60vh] space-y-2 overflow-y-auto text-xs">
              {(
                [
                  ["access_token", "Access Token"],
                  ["refresh_token", "Refresh Token"],
                  ["id_token", "ID Token"],
                  ["session_token", "Session Token"],
                  ["password", "密码"],
                  ["proxy", "代理"],
                  ["device_id", "Device ID"],
                  ["totp_secret", "TOTP Secret"],
                  ["cookie_header", "Cookie"],
                ] as const
              ).map(([key, label]) => {
                const value = detail[key];
                if (!value) return null;
                const text = String(value);
                return (
                  <div key={key} className="rounded-md border border-stone-200 p-2 dark:border-stone-700">
                    <div className="mb-1 font-medium text-muted-foreground">{label}</div>
                    <div className="break-all font-mono leading-relaxed">{text}</div>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="mt-1 h-6 px-1.5 text-xs"
                      onClick={() => {
                        void navigator.clipboard
                          .writeText(text)
                          .then(() => toast.success(`${label} 已复制`))
                          .catch(() => toast.error("复制失败"));
                      }}
                    >
                      <Copy className="size-3" />
                      复制
                    </Button>
                  </div>
                );
              })}
              {detail.extra && Object.keys(detail.extra).length > 0 ? (
                <div className="rounded-md border border-stone-200 p-2 dark:border-stone-700">
                  <div className="mb-1 font-medium text-muted-foreground">其他信息</div>
                  <pre className="whitespace-pre-wrap break-all font-mono leading-relaxed">
                    {JSON.stringify(detail.extra, null, 2)}
                  </pre>
                </div>
              ) : null}
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => setDetailOpen(false)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
