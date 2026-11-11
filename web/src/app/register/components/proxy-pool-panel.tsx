"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, RefreshCw, Upload } from "lucide-react";
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
import {
  fetchRegisterProxies,
  importRegisterProxies,
  type RegisterProxy,
  type RegisterProxyStats,
} from "@/lib/register-api";

const statusMeta: Record<string, { label: string; variant: "secondary" | "outline" | "danger" }> = {
  available: { label: "可用", variant: "secondary" },
  disabled: { label: "停用", variant: "outline" },
  exhausted: { label: "已满", variant: "danger" },
};

export function ProxyPoolPanel() {
  const [items, setItems] = useState<RegisterProxy[]>([]);
  const [stats, setStats] = useState<RegisterProxyStats | null>(null);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchRegisterProxies();
      setItems(data.items || []);
      setStats(data.stats ?? null);
    } catch (error) {
      toast.error(`加载失败：${(error as Error).message ?? error}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleImport = async (mode: "replace" | "append") => {
    if (!text.trim()) {
      toast.error("请先粘贴代理列表");
      return;
    }
    setSaving(true);
    try {
      const result = await importRegisterProxies(text, mode);
      toast.success(`已导入 ${result.total} 个代理`);
      setText("");
      await load();
    } catch (error) {
      toast.error(`导入失败：${(error as Error).message ?? error}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">导入代理</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">
            每行一个代理。<strong>覆盖</strong>会删掉不在列表里的代理（已有代理的用量保留）；
            <strong>追加</strong>只加新的。
          </p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            placeholder={"socks5://user:pass@host:port\nhttp://user:pass@host:port"}
            className="w-full rounded-md border border-stone-200 bg-white p-2 font-mono text-xs outline-none focus:border-stone-400 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-100"
          />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => handleImport("append")} disabled={saving}>
              追加
            </Button>
            <Button onClick={() => handleImport("replace")} disabled={saving}>
              {saving ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Upload className="size-4" />
              )}
              覆盖
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2">
          <CardTitle className="text-base">
            代理池
            {stats ? (
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                可用 {stats.available} / 停用 {stats.disabled} / 已满 {stats.exhausted}
              </span>
            ) : null}
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
                <TableHead>状态</TableHead>
                <TableHead>代理</TableHead>
                <TableHead className="w-20">类型</TableHead>
                <TableHead className="w-24">用量</TableHead>
                <TableHead className="w-20">失败</TableHead>
                <TableHead className="w-20">CF拦截</TableHead>
                <TableHead className="w-20">IPv6</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-sm text-muted-foreground">
                    {loading ? "加载中…" : "代理池是空的，先在上面导入"}
                  </TableCell>
                </TableRow>
              ) : (
                items.map((p) => {
                  const meta = statusMeta[p.status] ?? { label: p.status, variant: "outline" as const };
                  return (
                    <TableRow key={p.proxy}>
                      <TableCell>
                        <Badge variant={meta.variant}>{meta.label}</Badge>
                      </TableCell>
                      <TableCell className="max-w-[320px] truncate font-mono text-xs">
                        {p.proxy}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {p.proxy_type === "dynamic" ? "动态" : "静态"}
                      </TableCell>
                      <TableCell className="text-xs tabular-nums">
                        {p.used_count} / {p.max_uses || "∞"}
                      </TableCell>
                      <TableCell className="text-xs tabular-nums">{p.failed_count}</TableCell>
                      <TableCell className="text-xs tabular-nums">{p.cf_blocked}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {p.ipv6_status === "ok"
                          ? "支持"
                          : p.ipv6_status === "fail"
                            ? "不支持"
                            : "未测"}
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
