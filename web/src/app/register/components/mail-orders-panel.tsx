"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Copy,
  Inbox,
  LoaderCircle,
  Plus,
  RefreshCw,
  Trash2,
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
  bulkDeleteOrders,
  createMailOrder,
  deleteMailOrder,
  fetchMailOrders,
  fetchMailWallet,
  fetchOrderMails,
  setOrderDisabled,
  type MailOrder,
  type MailOrderMail,
} from "@/lib/register-api";

import { Pagination } from "./pagination";
import { copyTextWithToast } from "@/lib/clipboard";


/** Remail 服务端返回的订单状态。 */
const STATUS_META: Record<
  string,
  { label: string; variant: "secondary" | "outline" | "danger" }
> = {
  pending_payment: { label: "待支付", variant: "outline" },
  paid: { label: "已支付", variant: "secondary" },
  active: { label: "生效中", variant: "secondary" },
  completed: { label: "已完成", variant: "outline" },
  // consumed = 短效（code）订单已消耗。
  // 短效的 serviceToken 是一次性凭证，用完就失效（实测取件 401），
  // 所以状态必须如实标成「已消耗」，而不是继续挂在「生效中」。
  consumed: { label: "已消耗", variant: "outline" },
  refunded: { label: "已退款", variant: "outline" },
  failed: { label: "失败", variant: "danger" },
  closed: { label: "已关闭", variant: "outline" },
};

/** 邮箱类型（后缀）→ 可读说明。 */
const SUFFIX_TEXT: Record<string, string> = {
  outlook: "微软邮箱（随机后缀）",
  "outlook.com": "微软 outlook.com",
  "gmail.com": "Gmail 主邮箱",
  gmail_variant: "Gmail 变种",
  "icloud.com": "iCloud",
  proto: "Proto",
  domain: "域名邮箱",
};

function suffixText(row: MailOrder) {
  const s = row.suffix || "";
  if (!s) return "—";
  return SUFFIX_TEXT[s] ? `${SUFFIX_TEXT[s]}（${s}）` : s;
}

function formatTime(ts?: number | null) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/**
 * Remail 订单管理。
 *
 * 「邮箱配置」页是填参数（Key / 项目 ID / 后缀 / 模式）；
 * 这一页是看订单：下了什么邮箱、什么类型、花了多少钱、能不能再收一次件。
 *
 * Remail 的长效（purchase）订单可以**重复收件** —— 注册超时、想查历史验证码
 * 时不用重新下单（重新下单要再花钱）。
 */
export function MailOrdersPanel() {
  const [items, setItems] = useState<MailOrder[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [wallet, setWallet] = useState<{ balance: string; enabled?: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);

  // 手动下单表单
  const [suffix, setSuffix] = useState("");
  const [serviceMode, setServiceMode] = useState("purchase");
  const [supplyPolicy, setSupplyPolicy] = useState("private_first");

  // 收件箱弹窗
  const [mailsOpen, setMailsOpen] = useState(false);
  const [mailsLoading, setMailsLoading] = useState(false);
  const [mailsEmail, setMailsEmail] = useState("");
  const [mails, setMails] = useState<MailOrderMail[]>([]);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTargets, setDeleteTargets] = useState<string[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchMailOrders(pageSize, (page - 1) * pageSize);
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (error) {
      toast.error(`加载订单失败：${(error as Error).message ?? error}`);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize]);

  const loadWallet = useCallback(async () => {
    try {
      setWallet(await fetchMailWallet());
    } catch {
      setWallet(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void loadWallet();
  }, [loadWallet]);

  const handleCreate = async () => {
    setCreating(true);
    try {
      const result = await createMailOrder({
        suffix: suffix.trim() || undefined,
        service_mode: serviceMode,
        supply_policy: supplyPolicy,
      });
      toast.success(`下单成功：${result.email}`);
      setSuffix("");
      await load();
      await loadWallet();
    } catch (error) {
      toast.error(`下单失败：${(error as Error).message ?? error}`);
    } finally {
      setCreating(false);
    }
  };

  const openMails = async (order: MailOrder) => {
    setMailsOpen(true);
    setMailsLoading(true);
    setMailsEmail(order.email);
    setMails([]);
    try {
      const data = await fetchOrderMails(order.order_no);
      setMails(data.items || []);
      if (!data.items?.length) toast.info("这个订单目前还没有收到邮件");
    } catch (error) {
      toast.error(`取件失败：${(error as Error).message ?? error}`);
    } finally {
      setMailsLoading(false);
    }
  };

  const toggleDisabled = async (order: MailOrder) => {
    const next = !order.disabled;
    try {
      await setOrderDisabled(order.order_no, next);
      toast.success(next ? `已禁用 ${order.email}，不会再被自动复用` : `已启用 ${order.email}`);
      await load();
    } catch (error) {
      toast.error(`操作失败：${(error as Error).message ?? error}`);
    }
  };

  const removeOrders = async () => {
    try {
      const result = await bulkDeleteOrders(deleteTargets);
      toast.success(`已删除 ${result.deleted} 条`);
      setDeleteOpen(false);
      setDeleteTargets([]);
      setSelected(new Set());
      await load();
    } catch (error) {
      toast.error(`删除失败：${(error as Error).message ?? error}`);
    }
  };

  const copyEmail = async (email: string) => {
    await copyTextWithToast(email, "邮箱", "已复制邮箱");
  };

  const allSelected = items.length > 0 && items.every((o) => selected.has(o.order_no));
  const reusableCount = items.filter((o) => o.reusable).length;
  const disabledCount = items.filter((o) => o.disabled).length;
  const linkedCount = items.filter((o) => o.used_email).length;

  return (
    <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
      {/* 左：状态与下单 */}
      <Card className="h-fit">
        <CardHeader>
          <CardTitle className="text-base">状态与下单</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-2">
            <Badge variant="secondary">余额 {wallet?.balance ?? "—"}</Badge>
            <Badge variant="outline">共 {total} 单</Badge>
            <Badge variant="outline">已关联 {linkedCount}</Badge>
          </div>

          <div className="space-y-1.5 border-t pt-3">
            <Label htmlFor="order-suffix">邮箱后缀</Label>
            <Input
              id="order-suffix"
              value={suffix}
              onChange={(e) => setSuffix(e.target.value)}
              placeholder="留空用「邮箱配置」里的默认值"
            />
            <p className="text-xs text-muted-foreground">
              outlook = 微软白名单后缀随机；outlook.com = 精确匹配；
              domain = 域名邮箱；gmail_variant = Gmail 变种
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="order-mode">服务模式</Label>
            <Select value={serviceMode} onValueChange={setServiceMode}>
              <SelectTrigger id="order-mode">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="purchase">purchase（长效，可重复收件）</SelectItem>
                <SelectItem value="code">code（短效接码，一次性）</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="order-policy">库存策略</Label>
            <Select value={supplyPolicy} onValueChange={setSupplyPolicy}>
              <SelectTrigger id="order-policy">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="private_first">private_first（优先自有库存）</SelectItem>
                <SelectItem value="public_only">public_only（只用公共库存）</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <Button onClick={handleCreate} disabled={creating} className="w-full">
            {creating ? (
              <LoaderCircle className="size-4 animate-spin" />
            ) : (
              <Plus className="size-4" />
            )}
            下单
          </Button>

          <p className="text-xs text-muted-foreground">
            正常注册会自动下单，这里是给「测试连通性」和「补单」用的。每次下单都会扣费。
          </p>
          <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-200">
            Remail 不交付邮箱密码。如果 OpenAI 把这个地址判定为「已有账号」，
            会要求密码登录 → 必然失败。自有域名后缀成功率最高。
          </p>
        </CardContent>
      </Card>

      {/* 右：订单列表 */}
      <div className="space-y-4">
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2">
            <CardTitle className="text-base">
              订单列表
              <span className="ml-2 text-xs font-normal text-muted-foreground">共 {total} 单</span>
            </CardTitle>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={selected.size === 0}
                onClick={() => {
                  setDeleteTargets([...selected]);
                  setDeleteOpen(true);
                }}
              >
                <Trash2 className="size-4" />
                删除选中（{selected.size}）
              </Button>
              <Button variant="secondary" size="sm" onClick={() => void load()}>
                <RefreshCw className="size-4" />
                刷新
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div className="mb-3 flex flex-wrap gap-2">
              <Badge variant="secondary">可复用 {reusableCount}</Badge>
              <Badge variant="outline">已禁用 {disabledCount}</Badge>
              <Badge variant="outline">已关联账号 {linkedCount}</Badge>
            </div>

            <p className="mb-3 text-xs text-muted-foreground">
              长效（purchase）订单在没关联账号之前会被自动复用 —— 下次注册直接拿这个已付费的邮箱，
              不再重复下单扣费。禁用的订单不再被复用，但仍可手动收件。
            </p>

            <div className="overflow-x-auto">
            <Table className="min-w-[1180px]">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <Checkbox
                      checked={allSelected}
                      onCheckedChange={(v) => {
                        const next = new Set(selected);
                        items.forEach((o) => {
                          if (v) next.add(o.order_no);
                          else next.delete(o.order_no);
                        });
                        setSelected(next);
                      }}
                    />
                  </TableHead>
                  <TableHead className="whitespace-nowrap">邮箱</TableHead>
                  <TableHead className="w-40 whitespace-nowrap">邮箱类型</TableHead>
                  <TableHead className="w-20 whitespace-nowrap">模式</TableHead>
                  <TableHead className="w-36 whitespace-nowrap">状态</TableHead>
                  <TableHead className="w-20 whitespace-nowrap">尝试</TableHead>
                  <TableHead className="w-24">付费</TableHead>
                  <TableHead className="w-40">关联账号</TableHead>
                  <TableHead className="w-16">启用</TableHead>
                  <TableHead className="w-40">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={10} className="py-8 text-center text-sm text-muted-foreground">
                      {loading
                        ? "加载中…"
                        : "还没有 Remail 订单。注册一个号或手动下一单就会出现。"}
                    </TableCell>
                  </TableRow>
                ) : (
                  items.map((order) => {
                    const meta =
                      STATUS_META[order.order_status] ?? {
                        label: order.order_status || "—",
                        variant: "outline" as const,
                      };
                    return (
                      <TableRow key={order.order_no}>
                        <TableCell>
                          <Checkbox
                            checked={selected.has(order.order_no)}
                            onCheckedChange={(v) => {
                              const next = new Set(selected);
                              if (v) next.add(order.order_no);
                              else next.delete(order.order_no);
                              setSelected(next);
                            }}
                          />
                        </TableCell>
                        <TableCell className="max-w-[220px] truncate font-mono text-xs">
                          {order.email}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {suffixText(order)}
                        </TableCell>
                        <TableCell className="whitespace-nowrap">
                          <Badge variant={order.service_mode === "purchase" ? "secondary" : "outline"}>
                            {order.service_mode === "purchase" ? "长效" : "短效"}
                          </Badge>
                        </TableCell>
                        <TableCell className="whitespace-nowrap">
                          <Badge variant={meta.variant}>{meta.label}</Badge>
                          {order.disabled ? (
                            <Badge variant="danger" className="ml-1">
                              已禁用
                            </Badge>
                          ) : order.reusable ? (
                            <Badge variant="secondary" className="ml-1">
                              可复用
                            </Badge>
                          ) : order.used_email ? (
                            <Badge variant="outline" className="ml-1">
                              已用
                            </Badge>
                          ) : null}
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs tabular-nums">
                          {order.attempt_count} / {order.max_attempts}
                        </TableCell>
                        <TableCell className="text-xs tabular-nums">{order.pay_amount || "—"}</TableCell>
                        <TableCell className="max-w-[160px] truncate font-mono text-xs">
                          {order.used_email || "—"}
                        </TableCell>
                        <TableCell>
                          <Checkbox
                            checked={!order.disabled}
                            onCheckedChange={() => void toggleDisabled(order)}
                            aria-label="启用"
                          />
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-nowrap gap-0.5">
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 whitespace-nowrap px-1.5 text-xs"
                              onClick={() => void openMails(order)}
                            >
                              <Inbox className="size-3.5" />
                              收件箱
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 whitespace-nowrap px-1.5 text-xs"
                              onClick={() => void copyEmail(order.email)}
                            >
                              <Copy className="size-3.5" />
                              复制
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 whitespace-nowrap px-1.5 text-xs text-red-600 dark:text-red-400"
                              onClick={() => {
                                setDeleteTargets([order.order_no]);
                                setDeleteOpen(true);
                              }}
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
            </div>

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

      {/* 收件箱弹窗 */}
      <Dialog open={mailsOpen} onOpenChange={setMailsOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>收件箱 · {mailsEmail}</DialogTitle>
            <DialogDescription>
              长效（purchase）订单可以随时重复收件，不用重新下单。
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[50vh] overflow-y-auto">
            {mailsLoading ? (
              <p className="py-8 text-center text-sm text-muted-foreground">取件中…</p>
            ) : mails.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                这个订单还没有收到邮件
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-28">验证码</TableHead>
                    <TableHead>主题</TableHead>
                    <TableHead className="w-44">发件人</TableHead>
                    <TableHead>摘要</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {mails.map((mail, idx) => (
                    <TableRow key={mail.id ?? idx}>
                      <TableCell className="font-mono text-base font-semibold">
                        {mail.verificationCode || "—"}
                      </TableCell>
                      <TableCell className="max-w-[220px] truncate text-xs">
                        {mail.subject || "—"}
                      </TableCell>
                      <TableCell className="max-w-[180px] truncate text-xs text-muted-foreground">
                        {mail.sender || "—"}
                      </TableCell>
                      <TableCell className="max-w-[260px] truncate text-xs text-muted-foreground">
                        {mail.bodyPreview || "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setMailsOpen(false)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 删除确认 */}
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除订单记录</DialogTitle>
            <DialogDescription>
              确定删除选中的 {deleteTargets.length} 条订单本地记录？
              <br />
              只删本地记录，**不会去 Remail 退单或退款**（退款要走服务端的售后流程）。
              删除后这个邮箱不会再被复用，也看不到它了。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setDeleteOpen(false)}>
              取消
            </Button>
            <Button variant="destructive" onClick={() => void removeOrders()}>
              确定删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
