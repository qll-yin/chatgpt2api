"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Copy, Download, Eye, KeyRound, LoaderCircle, RefreshCw } from "lucide-react";
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
  fetchBindableProxies,
  fetchRegistered,
  fetchRegisteredAccessTokens,
  fetchRegisteredDetail,
  fetchRegisterProxies,
  importRegisteredToPool,
  markRegisteredAbnormal,
  reauthorizeRegisteredAccount,
  type RegisteredItem,
  type RegisterProxy,
} from "@/lib/register-api";
import { copyTextWithToast } from "@/lib/clipboard";

import { Pagination } from "./pagination";


/**
 * 注册结果：勾选账号 → 批量导入号池。
 *
 * 两种导入方式：
 *   1. 勾选下面的账号 → 「导入号池」（按 access_token----proxy 格式写入）
 *   2. 粘贴文本 → 「按文本导入」（格式：access_token----proxy，每行一个）
 *
 * ★ 号池绑定静态代理
 * ------------------
 * 注册时用的是**动态代理**（出口 IP 每次都变，防注册风控）。但账号导入
 * 号池后要长期使用，IP 不能每次都变 —— 同一账号在多个出口之间跳变，
 * 反而更容易触发使用侧风控。
 *
 * 所以导入时可以指定静态代理：
 *   - 「自动分配」→ 从静态代理池里挑（绑得少的优先），每个代理受绑定上限约束
 *   - 选一个具体代理 → 全部绑到它
 *   - 都不选 → 沿用注册时的动态代理（保持原行为）
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
  const [copyingTokens, setCopyingTokens] = useState(false);
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof fetchRegisteredDetail>>["item"] | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  // 号池绑定的静态代理
  const [bindable, setBindable] = useState<RegisterProxy[]>([]);
  const [bindMode, setBindMode] = useState<"none" | "register" | "static" | "manual">("static");
  const [bindProxy, setBindProxy] = useState("");

  // ── 刷新 401（重新授权）──
  const [reauthOpen, setReauthOpen] = useState(false);
  const [reauthEmail, setReauthEmail] = useState("");
  const [reauthing, setReauthing] = useState(false);
  // 代理来源：沿用原代理 / 代理池里的静态代理 / 手填
  const [reauthProxyMode, setReauthProxyMode] = useState<"keep" | "pool" | "manual">("keep");
  const [reauthPoolProxy, setReauthPoolProxy] = useState("");
  const [reauthManualProxy, setReauthManualProxy] = useState("");
  // 代理池里所有启用的静态代理（刷新不占绑定名额，所以不能用 bindable 列表）
  const [staticProxies, setStaticProxies] = useState<RegisterProxy[]>([]);

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

  // 号池绑定的静态代理列表（导入面板要显示各代理剩余名额）
  const loadBindable = useCallback(async () => {
    try {
      const data = await fetchBindableProxies();
      setBindable(data.items || []);
    } catch {
      // 拉不到就不显示绑定选项，不影响原有导入流程
      setBindable([]);
    }
  }, []);

  useEffect(() => {
    void loadBindable();
  }, [loadBindable]);

  // 刷新 401 时可选的静态代理（不按绑定名额过滤 —— 刷新不占名额）
  const loadStaticProxies = useCallback(async () => {
    try {
      const data = await fetchRegisterProxies();
      setStaticProxies(
        (data.items || []).filter(
          (p) => p.proxy_type === "static" && p.enabled && p.status !== "disabled",
        ),
      );
    } catch {
      setStaticProxies([]);
    }
  }, []);

  useEffect(() => {
    void loadStaticProxies();
  }, [loadStaticProxies]);

  const openReauth = (email: string) => {
    setReauthEmail(email);
    setReauthProxyMode("keep");
    setReauthPoolProxy("");
    setReauthManualProxy("");
    setReauthOpen(true);
  };

  const handleReauth = async () => {
    if (!reauthEmail) return;
    const proxy =
      reauthProxyMode === "pool"
        ? reauthPoolProxy
        : reauthProxyMode === "manual"
          ? reauthManualProxy.trim()
          : "";
    if (reauthProxyMode === "pool" && !proxy) {
      toast.error("请选择一个静态代理");
      return;
    }
    if (reauthProxyMode === "manual" && !proxy) {
      toast.error("请填写代理地址");
      return;
    }
    setReauthing(true);
    try {
      const { result } = await reauthorizeRegisteredAccount(reauthEmail, proxy);
      if (result.codex_refresh_token) {
        toast.success(
          `已刷新 ${result.email}：拿到新的 codex 凭证` +
            (result.proxy ? `（代理 ${result.proxy}）` : ""),
        );
      } else {
        toast.warning(
          `已刷新 ${result.email}，但没换到 codex 的 refresh_token` +
            (result.error ? `：${result.error}` : ""),
        );
      }
      setReauthOpen(false);
      await load();
    } catch (error) {
      toast.error(`刷新失败：${(error as Error).message ?? error}`);
    } finally {
      setReauthing(false);
    }
  };

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
      const result = await importRegisteredToPool([...selected], withProxy, {
        bindProxy: bindMode === "manual" ? bindProxy : undefined,
        autoBind: bindMode === "static",
        proxyMode:
          bindMode === "none"
            ? "none"
            : bindMode === "register"
              ? "register"
              : "static",
      });
      const bs = result.bind_stats;
      const bindHint = bs && bs.bound > 0
        ? `，绑定静态代理 ${bs.bound} 个`
        : bs && bs.skipped_no_quota > 0
          ? `，${bs.skipped_no_quota} 个因代理名额不足未绑定`
          : "";
      toast.success(
        `导入完成：新增 ${result.imported} 个，跳过 ${result.skipped} 个` +
          bindHint +
          (result.missing.length ? `，${result.missing.length} 个无凭证` : ""),
      );
      setSelected(new Set());
      await load();
      await loadBindable();
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

  const handleCopyTokens = async (withProxy: boolean) => {
    if (selectedCount === 0) {
      toast.error("请先勾选要复制的账号");
      return;
    }
    setCopyingTokens(true);
    try {
      // 列表接口只给 has_access_token（不泄露凭证），所以走单独的批量接口
      const data = await fetchRegisteredAccessTokens([...selected]);
      const rows = data.items.map((it) =>
        withProxy ? `${it.access_token}----${it.proxy || ""}` : it.access_token,
      );
      if (rows.length === 0) {
        toast.error("选中的账号都没有 access_token");
        return;
      }
      const ok = await copyTextWithToast(
        rows.join("\n"),
        withProxy ? "Token+代理" : "Token",
        `已复制 ${rows.length} 个账号的${withProxy ? " access_token----代理" : " access_token"}`,
      );
      if (ok && data.missing.length) {
        toast.warning(`${data.missing.length} 个账号没有 access_token，已跳过`);
      }
    } catch (error) {
      toast.error(`复制失败：${(error as Error).message ?? error}`);
    } finally {
      setCopyingTokens(false);
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
            {/* 入池代理：2 选 1（沿用注册代理 / 静态代理）+ 不带代理
                ⚠️ 动态代理永远不会进号池 —— 出口 IP 每次变，账号用起来
                   更容易触发风控，还会把代理流量包耗光。 */}
            <select
              className="h-8 rounded-lg border border-stone-200 bg-white px-2 text-xs text-stone-700 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-200"
              value={bindMode === "manual" ? `manual:${bindProxy}` : bindMode}
              onChange={(e) => {
                const value = e.target.value;
                if (value.startsWith("manual:")) {
                  setBindMode("manual");
                  setBindProxy(value.slice("manual:".length));
                } else {
                  setBindMode(value as "none" | "register" | "static");
                }
              }}
              title="注册用动态代理防风控；入池改绑静态代理，账号出口 IP 固定。动态代理不会进号池。"
            >
              <option value="static">入池时携带静态代理</option>
              <option value="register">入池时携带注册所用代理</option>
              <option value="none">入池不带代理</option>
              {bindable.map((p) => (
                <option key={p.proxy} value={`manual:${p.proxy}`}>
                  指定：{p.proxy} （可绑 {p.bind_remaining === -1 ? "∞" : p.bind_remaining}）
                </option>
              ))}
            </select>
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
            <Button
              size="sm"
              variant="outline"
              onClick={() => void handleCopyTokens(false)}
              disabled={copyingTokens || selectedCount === 0}
              title="复制选中账号的 access_token，每行一个"
            >
              {copyingTokens ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Copy className="size-4" />
              )}
              复制 Token（{selectedCount}）
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void handleCopyTokens(true)}
              disabled={copyingTokens || selectedCount === 0}
              title="复制成 access_token----代理 的导入格式（可直接粘到其它号池）"
            >
              <Copy className="size-4" />
              复制 Token+代理
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
                <TableHead className="w-40">操作</TableHead>
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
                      <div className="flex items-center gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 whitespace-nowrap px-1.5 text-xs"
                          onClick={() => void handleShowDetail(item.email)}
                        >
                          <Eye className="size-3.5" />
                          查看
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 whitespace-nowrap px-1.5 text-xs text-amber-700 hover:text-amber-800"
                          onClick={() => openReauth(item.email)}
                          title="凭证失效（401）时重新授权，换一份新的 access_token / refresh_token"
                        >
                          <KeyRound className="size-3.5" />
                          刷新401
                        </Button>
                      </div>
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
                        // ⚠️ 不能直接用 navigator.clipboard —— HTTP 下它是
                        //    undefined，会同步抛异常（Promise 链都建不起来），
                        //    表现就是「点了没反应、也没有提示」。
                        void copyTextWithToast(text, label);
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

      {/* 刷新 401：重新授权 */}
      <Dialog open={reauthOpen} onOpenChange={(v) => !reauthing && setReauthOpen(v)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>刷新 401（重新授权）</DialogTitle>
            <DialogDescription>
              用账号库里已有的密码 + 2FA 重新走一次登录与 Codex 授权，
              换一份新的 access_token / refresh_token / session。
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label>账号</Label>
              <Input value={reauthEmail} readOnly className="bg-muted/40" />
            </div>

            <div className="space-y-1.5">
              <Label>本次授权使用的代理</Label>
              <Select
                value={reauthProxyMode}
                onValueChange={(v) =>
                  setReauthProxyMode(v as "keep" | "pool" | "manual")
                }
              >
                <SelectTrigger className="h-9 rounded-xl border-stone-200 bg-white dark:border-stone-700 dark:bg-transparent">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="keep">沿用账号原来的代理</SelectItem>
                  <SelectItem value="pool">从代理池选一个静态代理</SelectItem>
                  <SelectItem value="manual">手动输入代理</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {reauthProxyMode === "pool" ? (
              <div className="space-y-1.5">
                <Label>静态代理</Label>
                {staticProxies.length === 0 ? (
                  <p className="text-xs text-amber-600">
                    代理池里没有启用的静态代理，先到「代理池」添加。
                  </p>
                ) : (
                  <Select value={reauthPoolProxy} onValueChange={setReauthPoolProxy}>
                    <SelectTrigger className="h-9 rounded-xl border-stone-200 bg-white dark:border-stone-700 dark:bg-transparent">
                      <SelectValue placeholder="选择一个静态代理" />
                    </SelectTrigger>
                    <SelectContent>
                      {staticProxies.map((p) => (
                        <SelectItem key={p.proxy} value={p.proxy}>
                          {p.proxy}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </div>
            ) : null}

            {reauthProxyMode === "manual" ? (
              <div className="space-y-1.5">
                <Label>代理地址</Label>
                <Input
                  value={reauthManualProxy}
                  onChange={(e) => setReauthManualProxy(e.target.value)}
                  placeholder="http://user:pass@host:port 或 socks5://host:port"
                  className="h-9 rounded-xl border-stone-200 bg-white dark:border-stone-700 dark:bg-transparent"
                />
              </div>
            ) : null}

            <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-6 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
              ⚠️ Codex 的 refresh_token 是单次使用 + 轮换制 —— 同一份凭证不能同时给
              两个号池用。刷新完请把新配置重新导出给目标池，否则那边还是 401。
              刷新需要该账号在注册时设置过密码（密码 + 2FA 可自动通过）。
            </p>
          </div>

          <DialogFooter>
            <Button
              variant="secondary"
              onClick={() => setReauthOpen(false)}
              disabled={reauthing}
            >
              取消
            </Button>
            <Button onClick={() => void handleReauth()} disabled={reauthing}>
              {reauthing ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <RefreshCw className="size-4" />
              )}
              {reauthing ? "重新授权中…" : "开始刷新"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
