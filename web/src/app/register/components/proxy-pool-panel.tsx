"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Copy,
  Gauge,
  LoaderCircle,
  Network,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
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
import { Input } from "@/components/ui/input";
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
  clearRegisterProxies,
  deleteRegisterProxy,
  fetchRegisterProxies,
  importRegisterProxies,
  resetRegisterProxyCf,
  resetRegisterProxyInvalidState,
  resetRegisterProxyUsage,
  testRegisterProxies,
  testRegisterProxiesIpv6,
  updateRegisterProxy,
  type ProxyTestResult,
  type RegisterProxy,
  type RegisterProxyStats,
} from "@/lib/register-api";

const statusMeta: Record<
  string,
  { label: string; variant: "secondary" | "outline" | "danger" }
> = {
  available: { label: "可用", variant: "secondary" },
  disabled: { label: "停用", variant: "outline" },
  exhausted: { label: "已满", variant: "danger" },
};

/** 该代理生效的协议（未写协议默认按 http 处理）。 */
function proxyScheme(p: string) {
  const m = /^(socks5h?|socks4|https?):\/\//i.exec(p.trim());
  return m ? m[1].toLowerCase() : "http（默认）";
}

/** 代理格式是否合法：[scheme://][user:pass@]host:port */
function isValidProxy(p: string) {
  return /^((socks5h?|socks4|https?):\/\/)?\S+:\d+$/i.test(p.trim());
}

export function ProxyPoolPanel() {
  const [items, setItems] = useState<RegisterProxy[]>([]);
  const [stats, setStats] = useState<RegisterProxyStats | null>(null);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testingAll, setTestingAll] = useState(false);
  const [ipv6Testing, setIpv6Testing] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  // proxy -> 测试结果
  const [testResults, setTestResults] = useState<Record<string, ProxyTestResult>>({});

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

  const invalidCount = items.filter((p) => !isValidProxy(p.proxy)).length;

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

  const loadIntoDraft = () => {
    setText(items.map((p) => p.proxy).join("\n"));
    toast.info("已把当前代理池载入编辑框，改完点「覆盖」");
  };

  const copyAll = async () => {
    const content = items.map((p) => p.proxy).join("\n");
    try {
      await navigator.clipboard.writeText(content);
      toast.success(`已复制 ${items.length} 个代理`);
    } catch {
      toast.error("复制失败，请手动选中文本框内容");
    }
  };

  const runTest = async (targets: string[]) => {
    if (targets.length === 0) return;
    setTestResults((prev) => {
      const next = { ...prev };
      for (const p of targets) next[p] = { ok: false };
      return next;
    });
    try {
      const data = await testRegisterProxies(targets);
      setTestResults((prev) => ({ ...prev, ...(data.results || {}) }));
      const okCount = Object.values(data.results || {}).filter((r) => r.ok).length;
      toast.success(`连通测试完成：${okCount} / ${targets.length} 个正常`);
    } catch (error) {
      toast.error(`测试失败：${(error as Error).message ?? error}`);
    }
  };

  const testAll = async () => {
    setTestingAll(true);
    try {
      await runTest(items.map((p) => p.proxy));
    } finally {
      setTestingAll(false);
    }
  };

  const testIpv6All = async () => {
    if (items.length === 0) return;
    setIpv6Testing(true);
    try {
      const data = await testRegisterProxiesIpv6(items.map((p) => p.proxy));
      setTestResults((prev) => ({ ...prev, ...(data.results || {}) }));
      const okCount = Object.values(data.results || {}).filter((r) => r.ipv6).length;
      toast.success(`IPv6 检测完成：${okCount} / ${items.length} 个支持`);
      if (data.items) {
        setItems(data.items);
        setStats(data.stats ?? null);
      }
    } catch (error) {
      toast.error(`IPv6 检测失败：${(error as Error).message ?? error}`);
    } finally {
      setIpv6Testing(false);
    }
  };

  const patch = async (
    proxy: string,
    body: Parameters<typeof updateRegisterProxy>[1],
    successMsg?: string,
  ) => {
    try {
      const data = await updateRegisterProxy(proxy, body);
      setItems(data.items || []);
      setStats(data.stats ?? null);
      if (successMsg) toast.success(successMsg);
    } catch (error) {
      toast.error(`操作失败：${(error as Error).message ?? error}`);
    }
  };

  const toggleEnabled = (row: RegisterProxy) => {
    void patch(
      row.proxy,
      { enabled: !row.enabled },
      row.enabled ? `已停用 ${row.proxy}` : `已启用 ${row.proxy}`,
    );
  };

  const changeType = (row: RegisterProxy, value: string) => {
    const t = value === "dynamic" ? "dynamic" : "static";
    void patch(
      row.proxy,
      { proxy_type: t },
      `${row.proxy} 已设为${t === "dynamic" ? "动态（每次换 IP）" : "静态（固定 IP）"}`,
    );
  };

  const changeLimit = (row: RegisterProxy, value: string) => {
    const n = Math.max(0, Number.parseInt(value, 10) || 0);
    void patch(
      row.proxy,
      { max_uses: n },
      `${row.proxy} 上限改为 ${n === 0 ? "不限" : n}`,
    );
  };

  const changeBindLimit = (row: RegisterProxy, value: string) => {
    const n = Math.max(0, Number.parseInt(value, 10) || 0);
    void patch(
      row.proxy,
      { bind_max_uses: n },
      `${row.proxy} 号池绑定上限改为 ${n === 0 ? "不限" : n}`,
    );
  };

  const resetOne = async (row: RegisterProxy) => {
    try {
      const data = await resetRegisterProxyUsage(row.proxy);
      setItems(data.items || []);
      setStats(data.stats ?? null);
      toast.success(`${row.proxy} 用量已清零`);
    } catch (error) {
      toast.error(`重置失败：${(error as Error).message ?? error}`);
    }
  };

  const resetAllUsage = async () => {
    try {
      const data = await resetRegisterProxyUsage();
      setItems(data.items || []);
      setStats(data.stats ?? null);
      toast.success("已重置全部用量");
    } catch (error) {
      toast.error(`重置失败：${(error as Error).message ?? error}`);
    }
  };

  const resetCf = async (row: RegisterProxy) => {
    try {
      const data = await resetRegisterProxyCf(row.proxy);
      setItems(data.items || []);
      setStats(data.stats ?? null);
      toast.success(`${row.proxy} 的 CF 拦截计数已清零`);
    } catch (error) {
      toast.error(`重置失败：${(error as Error).message ?? error}`);
    }
  };

  const resetInvalid = async (row: RegisterProxy) => {
    try {
      const data = await resetRegisterProxyInvalidState(row.proxy);
      setItems(data.items || []);
      setStats(data.stats ?? null);
      toast.success(`${row.proxy} 的会话失效计数已清零`);
    } catch (error) {
      toast.error(`重置失败：${(error as Error).message ?? error}`);
    }
  };

  const removeOne = async (row: RegisterProxy) => {
    try {
      const data = await deleteRegisterProxy(row.proxy);
      setItems(data.items || []);
      setStats(data.stats ?? null);
      toast.success("已删除");
    } catch (error) {
      toast.error(`删除失败：${(error as Error).message ?? error}`);
    }
  };

  const clearAll = async () => {
    try {
      const data = await clearRegisterProxies();
      setItems(data.items || []);
      setStats(data.stats ?? null);
      setClearOpen(false);
      toast.success(`已清空 ${data.removed} 个代理`);
    } catch (error) {
      toast.error(`清空失败：${(error as Error).message ?? error}`);
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
            每行一个：<code className="font-mono">[协议://][user:pass@]host:port</code>。
            不写协议默认按 <strong>HTTP 代理</strong>；SOCKS5 必须写{" "}
            <code className="font-mono">socks5://</code>。
            <strong>覆盖</strong>会删掉不在列表里的代理（已在池中的代理保留用量和启用状态）；
            <strong>追加</strong>只加新的。
          </p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            placeholder={"socks5://user:pass@host:port\nhttp://user:pass@host:port"}
            className="w-full rounded-md border border-stone-200 bg-white p-2 font-mono text-xs outline-none focus:border-stone-400 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-100"
          />
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={loadIntoDraft} disabled={items.length === 0}>
              载入当前池
            </Button>
            <Button variant="secondary" onClick={() => handleImport("append")} disabled={saving}>
              追加合并
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
                {invalidCount > 0 ? (
                  <span className="ml-1 text-red-500">· {invalidCount} 个格式异常</span>
                ) : null}
              </span>
            ) : null}
          </CardTitle>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" onClick={testAll} disabled={items.length === 0 || testingAll}>
              {testingAll ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Gauge className="size-4" />
              )}
              测试全部
            </Button>
            <Button variant="secondary" size="sm" onClick={testIpv6All} disabled={items.length === 0 || ipv6Testing}>
              {ipv6Testing ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Network className="size-4" />
              )}
              检测 IPv6
            </Button>
            <Button variant="secondary" size="sm" onClick={resetAllUsage} disabled={items.length === 0}>
              重置全部用量
            </Button>
            <Button variant="secondary" size="sm" onClick={copyAll} disabled={items.length === 0}>
              <Copy className="size-4" />
              复制全部
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setClearOpen(true)} disabled={items.length === 0}>
              <Trash2 className="size-4" />
              清空
            </Button>
            <Button variant="secondary" size="sm" onClick={() => void load()}>
              <RefreshCw className="size-4" />
              刷新
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">状态</TableHead>
                <TableHead>代理</TableHead>
                <TableHead className="w-24">协议</TableHead>
                <TableHead className="w-28">类型</TableHead>
                <TableHead className="w-24">CF拦截</TableHead>
                <TableHead className="w-28 whitespace-nowrap">会话失效</TableHead>
                <TableHead className="w-20">IPv6</TableHead>
                <TableHead className="w-16">启用</TableHead>
                <TableHead className="w-28">已用/上限</TableHead>
                <TableHead className="w-28">上限</TableHead>
                <TableHead className="w-28">已绑/绑定上限</TableHead>
                <TableHead className="w-24">绑定上限</TableHead>
                <TableHead className="w-28">连通性</TableHead>
                <TableHead className="w-64">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={12} className="py-8 text-center text-sm text-muted-foreground">
                    {loading ? "加载中…" : "代理池是空的，先在上面导入"}
                  </TableCell>
                </TableRow>
              ) : (
                items.map((p) => {
                  const meta = statusMeta[p.status] ?? { label: p.status, variant: "outline" as const };
                  const result = testResults[p.proxy];
                  return (
                    <TableRow key={p.proxy}>
                      <TableCell className="whitespace-nowrap">
                        <Badge variant={meta.variant}>{meta.label}</Badge>
                      </TableCell>
                      <TableCell className="max-w-[280px] truncate font-mono text-xs">
                        {p.proxy}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {proxyScheme(p.proxy)}
                      </TableCell>
                      <TableCell>
                        <Select
                          value={p.proxy_type === "dynamic" ? "dynamic" : "static"}
                          onValueChange={(v) => changeType(p, v)}
                        >
                          <SelectTrigger className="h-8 text-xs">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="static">静态</SelectItem>
                            <SelectItem value="dynamic">动态</SelectItem>
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell className="text-xs tabular-nums">
                        {p.cf_blocked > 0 ? (
                          <span className="text-red-600 dark:text-red-400">{p.cf_blocked} 次</span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs tabular-nums">
                        {p.invalid_state > 0 ? (
                          <span className="text-amber-600 dark:text-amber-400">
                            {p.invalid_state} 次
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {p.ipv6_status === "ok"
                          ? "支持"
                          : p.ipv6_status === "fail"
                            ? "不支持"
                            : "未测"}
                      </TableCell>
                      <TableCell>
                        <Checkbox
                          checked={p.enabled}
                          onCheckedChange={() => toggleEnabled(p)}
                          aria-label="启用"
                        />
                      </TableCell>
                      <TableCell className="text-xs tabular-nums">
                        {p.used_count} / {p.max_uses || "∞"}
                      </TableCell>
                      <TableCell>
                        <Input
                          type="number"
                          min={0}
                          defaultValue={p.max_uses}
                          className="h-8 px-2 text-xs"
                          onBlur={(e) => {
                            const n = Number.parseInt(e.target.value, 10);
                            if (!Number.isNaN(n) && n !== p.max_uses) changeLimit(p, e.target.value);
                          }}
                        />
                      </TableCell>
                      {/* 号池绑定用量：动态代理不给账号绑定，显示「不适用」 */}
                      <TableCell className="text-xs tabular-nums">
                        {p.proxy_type === "dynamic" ? (
                          <span className="text-muted-foreground">不适用</span>
                        ) : (
                          <span
                            className={
                              p.bind_status === "exhausted"
                                ? "text-amber-600 dark:text-amber-400"
                                : undefined
                            }
                          >
                            {p.bind_used_count ?? 0} / {p.bind_max_uses || "∞"}
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        {p.proxy_type === "dynamic" ? (
                          <span className="text-xs text-muted-foreground">—</span>
                        ) : (
                          <Input
                            type="number"
                            min={0}
                            defaultValue={p.bind_max_uses}
                            className="h-8 px-2 text-xs"
                            title="这个静态代理最多能绑多少个账号到号池（0 = 不限）"
                            onBlur={(e) => {
                              const n = Number.parseInt(e.target.value, 10);
                              if (!Number.isNaN(n) && n !== p.bind_max_uses) {
                                changeBindLimit(p, e.target.value);
                              }
                            }}
                          />
                        )}
                      </TableCell>
                      <TableCell className="text-xs">
                        {!result ? (
                          <span className="text-muted-foreground">未测</span>
                        ) : result.ok ? (
                          <span className="text-green-700 dark:text-green-400">
                            {result.latency_ms}ms
                            {result.ip ? ` · ${result.ip}` : ""}
                          </span>
                        ) : (
                          <span
                            className="text-red-600 dark:text-red-400"
                            title={result.error}
                          >
                            失败
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-nowrap gap-0.5">
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 whitespace-nowrap px-1.5 text-xs"
                            onClick={() => void runTest([p.proxy])}
                          >
                            测试
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 whitespace-nowrap px-1.5 text-xs"
                            onClick={() => void resetOne(p)}
                          >
                            清零
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 whitespace-nowrap px-1.5 text-xs"
                            disabled={!p.cf_blocked}
                            onClick={() => void resetCf(p)}
                          >
                            清CF
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 whitespace-nowrap px-1.5 text-xs"
                            disabled={!p.invalid_state}
                            title="清零 409 invalid_state 计数（换 IP 后手动清一下）"
                            onClick={() => void resetInvalid(p)}
                          >
                            清失效
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 whitespace-nowrap px-1.5 text-xs text-red-600 dark:text-red-400"
                            onClick={() => void removeOne(p)}
                          >
                            删除
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>

          <p className="mt-3 text-xs text-muted-foreground">
            批量注册时每次都会从池里动态取一个可用代理（优先挑用得少的）。达到上限或被停用的代理会自动跳过；
            池里全都不可用时跑号会停下并提示。上限只统计注册成功的号，失败的只累加失败次数、不占配额。
          </p>
        </CardContent>
      </Card>

      <Dialog open={clearOpen} onOpenChange={setClearOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>清空代理池</DialogTitle>
            <DialogDescription>
              确定清空全部 {items.length} 个代理？用量记录也会一并清除。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setClearOpen(false)}>
              取消
            </Button>
            <Button variant="destructive" onClick={() => void clearAll()}>
              确定清空
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
