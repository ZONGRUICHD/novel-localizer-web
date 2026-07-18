import type { ReactNode } from "react";
import { ApiError } from "../types/api";

export function DemoFlag() {
  return <span className="demo-flag">示范数据</span>;
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="empty-state">
      <span className="empty-state__mark" aria-hidden="true">栞</span>
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}

export function QueryState({ error, label }: { error: unknown; label: string }) {
  const message = error instanceof ApiError ? `${error.code}：${error.message}` : error instanceof Error ? error.message : "未知错误";
  return (
    <div className="query-error" role="alert">
      <strong>{label}暂不可用</strong>
      <span>{message}</span>
      <small>当前页面不会用示范内容替代真实服务。开发时可在地址后加入 <code>?demo=1</code> 查看界面。</small>
    </div>
  );
}

export function StatusMark({ tone = "neutral", children }: { tone?: "good" | "warning" | "danger" | "neutral"; children: ReactNode }) {
  return <span className={`status-mark status-mark--${tone}`}><i aria-hidden="true" />{children}</span>;
}
