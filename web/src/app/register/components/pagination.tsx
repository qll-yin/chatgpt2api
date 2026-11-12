"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/** 可选的每页条数 */
export const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;

/**
 * 列表分页条。
 *
 * 注册结果 / 运行记录 / 邮箱池 / 邮箱订单 都用它 —— 这些列表在后端
 * 都是 limit/offset 分页，前端统一在这里翻页。
 */
export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
}: {
  /** 当前页（从 1 开始） */
  page: number;
  pageSize: number;
  /** 总条数 */
  total: number;
  onPageChange: (page: number) => void;
  /** 切换每页条数（可选） */
  onPageSizeChange?: (size: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 pt-3 text-xs text-muted-foreground">
      <span>
        {total === 0 ? "暂无数据" : `第 ${from}-${to} 条 / 共 ${total} 条`}
      </span>
      <div className="flex items-center gap-2">
        {onPageSizeChange ? (
          <Select
            value={String(pageSize)}
            onValueChange={(v) => onPageSizeChange(Number(v))}
          >
            <SelectTrigger className="h-7 w-24 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PAGE_SIZE_OPTIONS.map((size) => (
                <SelectItem key={size} value={String(size)}>
                  每页 {size} 条
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
        <span className="whitespace-nowrap">
          第 {page} / {totalPages} 页
        </span>
        <Button
          variant="outline"
          size="sm"
          className="h-7 px-2"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          <ChevronLeft className="size-4" />
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-7 px-2"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
          <ChevronRight className="size-4" />
        </Button>
      </div>
    </div>
  );
}
