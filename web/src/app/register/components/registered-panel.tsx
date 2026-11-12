"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
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
  importRegisteredToPool,
  importTokenTextToPool,
  type RegisteredItem,
} from "@/lib/register-api";

import { Pagination } from "./pagination";

const PAGE_SIZE = 20;

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
  const [text, setText] = useState("");
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchRegistered(PAGE_SIZE, (page - 1) * PAGE_SIZE);
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

  const handleImportText = async () => {
    if (!text.trim()) {
      toast.error("请先粘贴要导入的内容");
      return;
    }
    setImporting(true);
    try {
      const result = await importTokenTextToPool(text);
      toast.success(`导入完成：新增 ${result.imported} 个，跳过 ${result.skipped} 个`);
      setText("");
      await load();
    } catch (error) {
      toast.error(`导入失败：${(error as Error).message ?? error}`);
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">导入外部账号</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">
            <strong>用途</strong>：把<strong>不是本机注册出来的</strong>账号批量灌进号池 ——
            比如别的机器上跑的注册机导出的文件、手头攒的账号、或从别处复制来的 token。
            本机注册成功的号走下面的「注册结果 → 勾选 → 导入号池」即可，不用走这里。
          </p>
          <p className="text-xs text-muted-foreground">
            每行一个账号，格式{" "}
            <code className="rounded bg-stone-100 px-1 dark:bg-stone-800">access_token----proxy</code>
            ，proxy 可省略（写成 <code className="rounded bg-stone-100 px-1 dark:bg-stone-800">access_token</code>{" "}
            即可）。
          </p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            placeholder={"eyJhbGciOi...\nsocks5://user:pass@host:port"}
            className="w-full rounded-md border border-stone-200 bg-white p-2 font-mono text-xs outline-none focus:border-stone-400 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-100"
          />
          <div className="flex justify-end">
            <Button onClick={handleImportText} disabled={importing}>
              {importing ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Download className="size-4" />
              )}
              导入
            </Button>
          </div>
        </CardContent>
      </Card>

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
                <TableHead>邮箱</TableHead>
                <TableHead className="w-24">access_token</TableHead>
                <TableHead className="w-24">refresh_token</TableHead>
                <TableHead>代理</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-sm text-muted-foreground">
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
    </div>
  );
}
