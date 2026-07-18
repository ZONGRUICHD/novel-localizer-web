import { useState } from "react";
import { useJobs } from "../api/queries";
import { api } from "../api/client";
import { DemoFlag, EmptyState, QueryState, StatusMark } from "../components/Common";
import type { JobState } from "../types/api";
import { formatDate, localeName } from "../utils/format";

const phaseLabel: Record<JobState, string> = {
  queued: "排队等待",
  validating: "验证原稿",
  parsing: "解析结构",
  awaiting_review: "等待处理",
  translating: "翻译",
  reviewing: "审校",
  assembling: "组装",
  validating_output: "验证",
  completed: "已完成",
  paused: "已暂停",
  failed: "失败",
  cancelled: "已取消",
};

export function JobsPage({ demoMode }: { demoMode: boolean }) {
  const jobs = useJobs(demoMode);
  const [actingJob, setActingJob] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function act(jobId: string, action: "pause" | "resume" | "cancel" | "retry") {
    if (demoMode) {
      setActionError("示范模式不会更改任务状态。");
      return;
    }
    setActingJob(jobId);
    setActionError(null);
    try {
      await api.jobAction(jobId, action);
      await jobs.refetch();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "任务操作失败。");
    } finally {
      setActingJob(null);
    }
  }
  return (
    <section aria-labelledby="jobs-title" className="page-section">
      <header className="page-heading">
        <div>
          <span className="eyebrow">WORK QUEUE</span>
          <h1 id="jobs-title">任务</h1>
          <p>任务由后台持续执行，关闭浏览器不会中断。</p>
        </div>
        {demoMode && <DemoFlag />}
      </header>
      <ol className="phase-key" aria-label="翻译任务阶段">
        {[
          ["解析结构", "01"], ["建立上下文", "02"], ["翻译", "03"], ["审校", "04"], ["组装", "05"], ["验证", "06"],
        ].map(([label, number]) => <li key={label}><span>{number}</span>{label}</li>)}
      </ol>
      {actionError && <p className="form-message form-message--error" role="alert">{actionError}</p>}

      {jobs.isLoading && <div className="ruled-loader" role="status">正在读取任务队列…</div>}
      {jobs.error && <QueryState error={jobs.error} label="任务队列" />}
      {!jobs.isLoading && !jobs.error && (jobs.data?.items.length ?? 0) === 0 && (
        <EmptyState title="当前没有任务">从书稿的“翻译”页启动简中或繁中任务。</EmptyState>
      )}
      {(jobs.data?.items.length ?? 0) > 0 && (
        <div className="job-list" role="list">
          {jobs.data?.items.map((job) => {
            const progressKnown = job.total_blocks !== null && job.total_blocks > 0;
            const progress = progressKnown ? job.completed_blocks / job.total_blocks! : 0;
            const tone = job.state === "failed" ? "danger" : job.state === "awaiting_review" ? "warning" : job.state === "completed" ? "good" : "neutral";
            return (
              <article className="job-row" role="listitem" key={job.id}>
                <div className="job-row__title">
                  <strong lang="ja">{job.book_title}</strong>
                  <span>{localeName(job.locale)}</span>
                </div>
                <div className="job-row__stage">
                  <StatusMark tone={tone}>{phaseLabel[job.state]}</StatusMark>
                  <p>{job.current_section ?? "尚未进入章节"}</p>
                </div>
                <div className="job-row__progress">
                  {progressKnown ? (
                    <><progress value={progress} max="1"><span>{Math.round(progress * 100)}%</span></progress><small>{job.completed_blocks} / {job.total_blocks} 段</small></>
                  ) : <small>总段数尚未确定</small>}
                </div>
                <div className="job-row__meta">
                  <span>{job.issue_count ? `${job.issue_count} 个问题` : "无待处理问题"}</span>
                  <time dateTime={job.updated_at}>{formatDate(job.updated_at)}</time>
                </div>
                <div className="job-row__actions">
                  {job.state === "awaiting_review" && <button className="text-button" type="button" disabled={actingJob === job.id} onClick={() => void act(job.id, "resume")}>确认继续</button>}
                  {job.state === "paused" && <button className="text-button" type="button" disabled={actingJob === job.id} onClick={() => void act(job.id, "resume")}>继续</button>}
                  {job.state === "failed" && <button className="text-button" type="button" disabled={actingJob === job.id} onClick={() => void act(job.id, "retry")}>重试</button>}
                  {!["awaiting_review", "paused", "failed", "completed", "cancelled"].includes(job.state) && <button className="text-button" type="button" disabled={actingJob === job.id} onClick={() => void act(job.id, "pause")}>暂停</button>}
                  {!(["completed", "cancelled"] as JobState[]).includes(job.state) && <button className="text-button text-button--muted" type="button" disabled={actingJob === job.id} onClick={() => void act(job.id, "cancel")}>取消</button>}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
