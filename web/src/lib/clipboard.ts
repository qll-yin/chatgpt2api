/**
 * 安全的剪贴板写入。
 *
 * ⚠️ 为什么不能直接用 `navigator.clipboard`
 * -----------------------------------------
 * `navigator.clipboard` 只在**安全上下文**（HTTPS 或 localhost）下存在。
 * 自建服务经常是 `http://<服务器IP>:3008` 这种明文 HTTP —— 此时
 * `navigator.clipboard` 是 `undefined`。
 *
 * 直接写 `navigator.clipboard.writeText(text)` 会在**同步**阶段抛
 * TypeError（读 undefined 的属性），Promise 链根本建立不起来 ——
 * 于是 `.then()` / `.catch()` 都不会执行：**按钮点了没反应，也没有提示**。
 * 这正是之前「账户详情里的复制按钮点击没反应」的原因。
 *
 * 所以这里：
 *   1. 先判断 API 是否存在（存在才用，权限拒绝等异常落到降级）
 *   2. 降级用 textarea + execCommand('copy')，在旧浏览器/HTTP 下也能用
 *   3. 全程 try/catch，失败时由调用方提示
 */

/** 复制文本到剪贴板。返回是否成功。 */
export async function copyText(text: string): Promise<boolean> {
  const value = String(text ?? "");
  if (!value) return false;

  // ① 标准 API（仅安全上下文可用）
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // 权限被拒 / 文档未聚焦 —— 落降级
  }

  // ② 降级：textarea + execCommand
  if (typeof document === "undefined") return false;
  let el: HTMLTextAreaElement | null = null;
  try {
    el = document.createElement("textarea");
    el.value = value;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.top = "-9999px";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.select();
    el.setSelectionRange(0, value.length);
    const ok = document.execCommand("copy");
    return Boolean(ok);
  } catch {
    return false;
  } finally {
    try {
      el?.remove();
    } catch {
      /* 忽略 */
    }
  }
}

/** 复制文本并给出 toast 反馈（成功/失败都有提示）。 */
export async function copyTextWithToast(
  text: string,
  label = "内容",
  successMessage?: string,
): Promise<boolean> {
  const ok = await copyText(text);
  if (ok) {
    const { toast } = await import("sonner");
    toast.success(successMessage ?? `${label} 已复制`);
  } else {
    const { toast } = await import("sonner");
    toast.error(`复制失败，请手动选中${label}复制`);
  }
  return ok;
}
