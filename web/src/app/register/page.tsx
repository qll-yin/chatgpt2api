"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { AutoRegisterPanel } from "./components/auto-register-panel";
import { BatchRegisterPanel } from "./components/batch-register-panel";
import { MailConfigPanel } from "./components/mail-config-panel";
import { MailOrdersPanel } from "./components/mail-orders-panel";
import { MailPoolPanel } from "./components/mail-pool-panel";
import { ProxyPoolPanel } from "./components/proxy-pool-panel";
import { RegisteredPanel } from "./components/registered-panel";
import { RunsPanel } from "./components/runs-panel";
import { SmsConfigPanel } from "./components/sms-config-panel";

/**
 * 注册管理。
 *
 * 九个标签页：
 *   批量注册 —— 起批量任务、实时日志、暂停/恢复/停止
 *   注册结果 —— 已注册的账号，勾选后批量导入号池 / 标注异常 / 看详情
 *   运行记录 —— 按批次看每一轮的邮箱数与完整日志
 *   代理池   —— 代理的启用/上限/用量
 *   邮箱配置 —— 邮箱来源与各 provider 的配置项
 *   邮箱池   —— 导入邮箱列表（outlook / icloud 中转）
 *   邮箱订单 —— Remail 订单（下手、取件、复用状态）
 *   接码配置 —— SMS 接码（取 refresh_token 时命中 add-phone 要用）
 *   自动补充 —— 号池低于阈值时自动注册
 */
export default function RegisterPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  const [tab, setTab] = useState("batch");

  if (isCheckingAuth) {
    return <div className="p-8 text-sm text-muted-foreground">加载中…</div>;
  }
  if (!session || session.role !== "admin") {
    return <div className="p-8 text-sm text-muted-foreground">仅管理员可访问</div>;
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-6 space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">注册管理</h1>
        <p className="text-sm text-muted-foreground">
          批量注册账号、查看注册结果，并直接导入到号池
        </p>
      </header>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList variant="line">
          <TabsTrigger value="batch">批量注册</TabsTrigger>
          <TabsTrigger value="registered">注册结果</TabsTrigger>
          <TabsTrigger value="runs">运行记录</TabsTrigger>
          <TabsTrigger value="proxies">代理池</TabsTrigger>
          <TabsTrigger value="mail">邮箱配置</TabsTrigger>
          <TabsTrigger value="pool">邮箱池</TabsTrigger>
          <TabsTrigger value="orders">邮箱订单</TabsTrigger>
          <TabsTrigger value="sms">接码配置</TabsTrigger>
          <TabsTrigger value="auto">自动补充</TabsTrigger>
        </TabsList>

        <TabsContent value="batch" className="pt-4">
          <BatchRegisterPanel />
        </TabsContent>
        <TabsContent value="registered" className="pt-4">
          <RegisteredPanel />
        </TabsContent>
        <TabsContent value="runs" className="pt-4">
          <RunsPanel />
        </TabsContent>
        <TabsContent value="proxies" className="pt-4">
          <ProxyPoolPanel />
        </TabsContent>
        <TabsContent value="mail" className="pt-4">
          <MailConfigPanel />
        </TabsContent>
        <TabsContent value="pool" className="pt-4">
          <MailPoolPanel />
        </TabsContent>
        <TabsContent value="orders" className="pt-4">
          <MailOrdersPanel />
        </TabsContent>
        <TabsContent value="sms" className="pt-4">
          <SmsConfigPanel />
        </TabsContent>
        <TabsContent value="auto" className="pt-4">
          <AutoRegisterPanel />
        </TabsContent>
      </Tabs>
    </div>
  );
}
