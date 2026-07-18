import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useCreateProject, useExports, useSegments, useUpdateSegment } from "../api/queries";
import { DemoFlag, QueryState, StatusMark } from "../components/Common";
import { Icons } from "../components/Icons";
import type { BookSummary, Locale, Segment } from "../types/api";
import { formatDate, localeName } from "../utils/format";

type BookTab = "overview" | "translation" | "preview" | "export";

const tabLabels: Record<BookTab, string> = {
  overview: "概览",
  translation: "翻译",
  preview: "预览",
  export: "导出",
};

export function BookWorkspace({ book, demoMode, onBack }: { book: BookSummary; demoMode: boolean; onBack: () => void }) {
  const [tab, setTab] = useState<BookTab>("translation");
  const [locale, setLocale] = useState<Locale>(book.target_locales[0] ?? "zh-CN");
  const segments = useSegments(book.id, locale, demoMode);

  function handleTabKey(event: React.KeyboardEvent<HTMLDivElement>) {
    const tabs = Object.keys(tabLabels) as BookTab[];
    const index = tabs.indexOf(tab);
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : (index - 1 + tabs.length) % tabs.length;
    setTab(tabs[next]);
    document.getElementById(`book-tab-${tabs[next]}`)?.focus();
  }

  return (
    <section className="book-workspace" aria-labelledby="workspace-title">
      <header className="workspace-header">
        <button className="back-button" type="button" onClick={onBack}><Icons.back />返回书库</button>
        <div className="workspace-title">
          <span className={`book-spine book-spine--${book.source_format}`} aria-hidden="true">{book.source_format.toUpperCase()}</span>
          <div>
            <p>{book.author ?? "作者信息未填写"}</p>
            <h1 id="workspace-title" lang={book.source_language}>{book.title}</h1>
          </div>
        </div>
        <div className="workspace-meta">
          {demoMode && <DemoFlag />}
          <label className="compact-select"><span>版本</span><select value={locale} onChange={(event) => setLocale(event.target.value as Locale)}>{(["zh-CN", "zh-TW"] as Locale[]).map((item) => <option key={item} value={item}>{localeName(item)}{book.target_locales.includes(item) ? "" : " · 未建立"}</option>)}</select></label>
        </div>
      </header>

      <div className="book-tabs" role="tablist" aria-label="书稿工作区" onKeyDown={handleTabKey}>
        {(Object.keys(tabLabels) as BookTab[]).map((value) => (
          <button
            key={value}
            id={`book-tab-${value}`}
            type="button"
            role="tab"
            aria-selected={tab === value}
            aria-controls={`book-panel-${value}`}
            tabIndex={tab === value ? 0 : -1}
            onClick={() => setTab(value)}
          >{tabLabels[value]}</button>
        ))}
      </div>

      <div id={`book-panel-${tab}`} role="tabpanel" aria-labelledby={`book-tab-${tab}`} tabIndex={0} className="workspace-panel">
        {tab === "overview" && <Overview book={book} locale={locale} />}
        {tab === "translation" && <TranslationEditor book={book} locale={locale} demoMode={demoMode} projectId={segments.data?.project_id ?? null} segments={segments.data?.items ?? []} loading={segments.isLoading} error={segments.error} onProjectCreated={() => void segments.refetch()} />}
        {tab === "preview" && <BookPreview book={book} locale={locale} segments={segments.data?.items ?? []} />}
        {tab === "export" && <Exports book={book} locale={locale} demoMode={demoMode} />}
      </div>
    </section>
  );
}

function Overview({ book, locale }: { book: BookSummary; locale: Locale }) {
  return (
    <div className="overview-layout">
      <section className="overview-lead">
        <span className="eyebrow">MANUSCRIPT</span>
        <h2>出版准备概览</h2>
        <p>原稿结构与资源已经进入统一文档模型。翻译版本相互独立，繁中不会由简中转码。</p>
      </section>
      <dl className="overview-facts">
        <div><dt>原稿格式</dt><dd>{book.source_format.toUpperCase()}</dd></div>
        <div><dt>原稿语言</dt><dd lang={book.source_language}>{book.source_language === "ja" ? "日本語" : book.source_language}</dd></div>
        <div><dt>当前译本</dt><dd>{localeName(locale)}</dd></div>
        <div><dt>封面策略</dt><dd>{book.cover_policy === "preserve" ? "原样保留" : book.cover_policy === "replace" ? "替代封面" : "不使用封面"}</dd></div>
        <div><dt>章节</dt><dd>{book.section_count === null ? "等待解析" : `${book.section_count} 章`}</dd></div>
        <div><dt>最近修改</dt><dd>{formatDate(book.updated_at)}</dd></div>
      </dl>
      <section className="overview-process" aria-labelledby="overview-process-title">
        <h3 id="overview-process-title">处理路径</h3>
        <ol><li className="done"><span>01</span>解析结构</li><li className="done"><span>02</span>建立上下文</li><li className="active"><span>03</span>翻译与审校</li><li><span>04</span>组装与验证</li></ol>
      </section>
    </div>
  );
}

interface EditorProps {
  book: BookSummary;
  locale: Locale;
  demoMode: boolean;
  projectId: string | null;
  segments: Segment[];
  loading: boolean;
  error: unknown;
  onProjectCreated: () => void;
}

function TranslationEditor({ book, locale, demoMode, projectId, segments, loading, error, onProjectCreated }: EditorProps) {
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const updateSegment = useUpdateSegment(book.id, projectId, demoMode);
  const createProject = useCreateProject(book.id, book.title, locale, demoMode);

  async function createEdition(
    cover: "preserve" | "replace" | "none",
    coverFile: File | null,
    onUploadProgress: (progress: number) => void,
  ) {
    let replacementCoverUploadId: string | undefined;
    if (cover === "replace") {
      if (!coverFile) throw new Error("请选择 JPEG 或 PNG 替代封面。");
      replacementCoverUploadId = (await api.uploadBook(coverFile, onUploadProgress, "cover")).upload_id;
    }
    await createProject.mutateAsync({ cover, replacementCoverUploadId });
    onProjectCreated();
  }

  useEffect(() => {
    if (!selectedId && segments[0]) setSelectedId(segments[0].id);
    setDrafts((current) => {
      const next = { ...current };
      for (const segment of segments) if (!(segment.id in next)) next[segment.id] = segment.target_text;
      return next;
    });
  }, [segments, selectedId]);

  const selected = segments.find((segment) => segment.id === selectedId) ?? segments[0] ?? null;
  const sections = useMemo(() => {
    const map = new Map<string, { id: string; title: string; segments: Segment[] }>();
    for (const segment of segments) {
      const current = map.get(segment.section_id) ?? { id: segment.section_id, title: segment.section_title, segments: [] };
      current.segments.push(segment);
      map.set(segment.section_id, current);
    }
    return [...map.values()];
  }, [segments]);

  if (loading) return <div className="ruled-loader" role="status">正在读取译稿…</div>;
  if (error) return <QueryState error={error} label="翻译编辑台" />;
  if (!projectId) return (
    <ProjectSetup
      locale={locale}
      pending={createProject.isPending}
      error={createProject.error}
      onCreate={createEdition}
    />
  );
  if (!segments.length) return <div className="empty-editor">译稿项目已建立，但还没有可编辑段落。请先启动翻译任务，或等待结构解析完成。</div>;

  return (
    <div className={`translation-editor ${leftOpen ? "" : "translation-editor--left-closed"} ${rightOpen ? "" : "translation-editor--right-closed"}`}>
      <aside className="chapter-pane" aria-label="章节与段落">
        <header><div><span className="pane-kicker">章节</span><strong>{sections.length} 章</strong></div><button className="pane-toggle" type="button" onClick={() => setLeftOpen(false)} aria-label="收起章节栏"><Icons.panel /></button></header>
        <nav aria-label="章节目录">
          {sections.map((section) => {
            const warningCount = section.segments.filter((segment) => segment.warnings.length).length;
            return (
              <div className="chapter-group" key={section.id}>
                <button type="button" className="chapter-title" onClick={() => setSelectedId(section.segments[0]?.id ?? null)}>
                  <span lang="ja">{section.title}</span>{warningCount > 0 && <b aria-label={`${warningCount} 个问题`}>{warningCount}</b>}
                </button>
                <ol>
                  {section.segments.map((segment) => <li key={segment.id}><button type="button" className={segment.id === selected?.id ? "active" : ""} aria-current={segment.id === selected?.id ? "true" : undefined} onClick={() => setSelectedId(segment.id)}><span>{String(segment.ordinal).padStart(2, "0")}</span><i className={`segment-state segment-state--${segment.status}`} aria-label={segment.status} /></button></li>)}
                </ol>
              </div>
            );
          })}
        </nav>
      </aside>

      {!leftOpen && <button className="restore-pane restore-pane--left" type="button" onClick={() => setLeftOpen(true)} aria-label="展开章节栏"><Icons.panel /></button>}

      <main className="manuscript-pane" aria-label="日中逐段对照">
        <header className="manuscript-toolbar">
          <div><span lang="ja">日本語</span><i aria-hidden="true" /><span lang={locale}>{localeName(locale)}</span></div>
          <p><kbd>⌘</kbd><span>+</span><kbd>S</kbd> 保存当前段</p>
        </header>
        <div className="segment-sheet">
          {segments.map((segment) => {
            const draft = drafts[segment.id] ?? segment.target_text;
            const changed = draft !== segment.target_text;
            return (
              <article className={`segment-pair ${selected?.id === segment.id ? "segment-pair--selected" : ""}`} id={`segment-${segment.id}`} key={segment.id} onFocus={() => setSelectedId(segment.id)}>
                <div className="segment-number"><span>{String(segment.ordinal).padStart(2, "0")}</span><i className={`segment-state segment-state--${segment.status}`} /></div>
                <div className="source-copy" lang="ja"><p>{segment.source_text}</p></div>
                <div className="target-copy">
                  <label><span className="visually-hidden">第 {segment.ordinal} 段{localeName(locale)}译文</span><textarea lang={locale} value={draft} onChange={(event) => setDrafts((current) => ({ ...current, [segment.id]: event.target.value }))} rows={Math.max(2, Math.ceil(draft.length / 30))} spellCheck /></label>
                  <div className="segment-actions">
                    <button className="quiet-icon-button" type="button" aria-label={segment.locked ? "解除人工锁定" : "锁定人工译文"} onClick={() => updateSegment.mutate({ segmentId: segment.id, text: draft, locked: !segment.locked })}>{segment.locked ? <Icons.lock /> : <Icons.unlock />}</button>
                    <button className="text-button" type="button" disabled={!changed || updateSegment.isPending} onClick={() => updateSegment.mutate({ segmentId: segment.id, text: draft, locked: segment.locked })}>{changed ? "保存修改" : segment.locked ? "人工锁定" : "已保存"}</button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      </main>

      {!rightOpen && <button className="restore-pane restore-pane--right" type="button" onClick={() => setRightOpen(true)} aria-label="展开检查栏"><Icons.panel /></button>}

      <aside className="context-pane" aria-label="术语、口吻与检查">
        <header><div><span className="pane-kicker">校记</span><strong>当前段</strong></div><button className="pane-toggle" type="button" onClick={() => setRightOpen(false)} aria-label="收起检查栏"><Icons.panel /></button></header>
        {selected && (
          <div className="context-scroll">
            <section><h3>检查</h3>{selected.warnings.length ? <ul className="warning-list">{selected.warnings.map((warning) => <li key={warning}><Icons.warning /><span>{warning}</span></li>)}</ul> : <p className="clear-note"><Icons.check />当前段没有自动检查警告。</p>}</section>
            <section><h3>术语</h3>{selected.terminology.length ? <dl className="term-list">{selected.terminology.map((term) => <div key={term.source}><dt lang="ja">{term.source}</dt><dd lang={locale}>{term.target}{term.note && <small>{term.note}</small>}</dd></div>)}</dl> : <p className="muted-copy">当前段没有命中的术语。</p>}</section>
            <section><h3>口吻与上下文</h3><dl className="context-facts"><div><dt>叙述</dt><dd>第一人称</dd></div><div><dt>语气</dt><dd>克制、日常</dd></div><div><dt>人物状态</dt><dd>本章内连续</dd></div></dl></section>
            <section>
              <h3>候选版本</h3>
              {selected.revisions.length ? (
                <div className="revision-list">
                  {selected.revisions.map((revision) => (
                    <article key={revision.id}>
                      <header>
                        <span>{revision.kind === "human" ? "人工版本" : revision.kind === "reviewed" ? "审校稿" : revision.kind === "candidate" ? "重译候选" : "模型初稿"}</span>
                        <button type="button" onClick={() => setDrafts((current) => ({ ...current, [selected.id]: revision.text }))}>载入</button>
                      </header>
                      <p lang={locale}>{revision.text}</p>
                      {revision.kind === "candidate" && (
                        <button
                          className="adopt-candidate"
                          type="button"
                          disabled={updateSegment.isPending}
                          onClick={() => {
                            setDrafts((current) => ({ ...current, [selected.id]: revision.text }));
                            updateSegment.mutate({ segmentId: selected.id, text: revision.text, locked: true });
                          }}
                        >采纳并锁定为人工译文</button>
                      )}
                    </article>
                  ))}
                </div>
              ) : <p className="muted-copy">没有其他候选。</p>}
              <button className="button button--full" type="button" onClick={() => !demoMode && projectId && void api.retranslateSegment(book.id, projectId, selected.id)}><Icons.refresh />生成重译候选</button>
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}

function ProjectSetup({
  locale,
  pending,
  error,
  onCreate,
}: {
  locale: Locale;
  pending: boolean;
  error: Error | null;
  onCreate: (
    cover: "preserve" | "replace" | "none",
    coverFile: File | null,
    onUploadProgress: (progress: number) => void,
  ) => Promise<void>;
}) {
  const [cover, setCover] = useState<"preserve" | "replace" | "none">("preserve");
  const [coverFile, setCoverFile] = useState<File | null>(null);
  const [progress, setProgress] = useState(0);
  const [localError, setLocalError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setLocalError(null);
    setBusy(true);
    try {
      await onCreate(cover, coverFile, setProgress);
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : "建立译稿失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="project-setup" aria-labelledby="project-setup-title">
      <span className="eyebrow">NEW EDITION</span>
      <h2 id="project-setup-title">建立{localeName(locale)}译稿</h2>
      <p>这个地区版本会拥有独立术语、审校记录与导出文件；不会从另一个中文版本转码。</p>
      <fieldset>
        <legend>封面策略</legend>
        <label><input type="radio" name="cover-policy" value="preserve" checked={cover === "preserve"} onChange={() => setCover("preserve")} /><span><strong>原样保留</strong><small>逐字节复制原书封面</small></span></label>
        <label><input type="radio" name="cover-policy" value="replace" checked={cover === "replace"} onChange={() => setCover("replace")} /><span><strong>替换封面</strong><small>上传自己的 JPEG 或 PNG</small></span></label>
        <label><input type="radio" name="cover-policy" value="none" checked={cover === "none"} onChange={() => setCover("none")} /><span><strong>不使用封面</strong><small>导出时省略封面页</small></span></label>
      </fieldset>
      {cover === "replace" && (
        <label className="cover-file-field">
          <span>替代封面文件</span>
          <input type="file" accept=".jpg,.jpeg,.png,image/jpeg,image/png" required onChange={(event) => { setCoverFile(event.target.files?.[0] ?? null); setProgress(0); }} />
          <small>{coverFile ? `${coverFile.name} · ${(coverFile.size / 1024 / 1024).toFixed(2)} MiB` : "JPEG 或 PNG；文件类型与扩展名必须一致。"}</small>
        </label>
      )}
      {(busy && cover === "replace") && <div className="cover-upload-progress" role="status"><span>{progress < 0.1 ? "计算封面校验值" : "上传替代封面"}</span><progress value={progress} max="1" /></div>}
      {(localError || error) && <p className="form-message form-message--error" role="alert">{localError ?? error?.message}</p>}
      <button className="button button--primary" type="button" disabled={pending || busy || (cover === "replace" && !coverFile)} onClick={() => void submit()}>{pending || busy ? "正在建立…" : `建立${localeName(locale)}译稿`}</button>
      <small>封面文件只保存到私人源站，不会发送给模型。</small>
    </section>
  );
}

function BookPreview({ book, locale, segments }: { book: BookSummary; locale: Locale; segments: Segment[] }) {
  return (
    <div className="preview-workspace">
      <aside className="preview-controls"><span className="eyebrow">HORIZONTAL PROOF</span><h2>横排校样</h2><p>预览为流式重排效果，不承诺复刻原 PDF 页面。</p><dl><div><dt>成品尺寸</dt><dd>A5</dd></div><div><dt>正文</dt><dd>思源宋体</dd></div><div><dt>排版</dt><dd>从左到右 · 横排</dd></div></dl></aside>
      <div className="proof-stage">
        <article className="proof-page" lang={locale} aria-label={`${book.title} 横排预览`}>
          <header><span>{book.title}</span><i /></header>
          <h2>{segments[0]?.section_title ?? "章节预览"}</h2>
          {segments.map((segment) => <p key={segment.id}>{segment.target_text || "（本段尚未翻译）"}</p>)}
          <footer><i /> <span>— 12 —</span> <i /></footer>
        </article>
      </div>
    </div>
  );
}

function Exports({ book, locale, demoMode }: { book: BookSummary; locale: Locale; demoMode: boolean }) {
  const exports = useExports(book.id, locale, demoMode);
  const [message, setMessage] = useState<string | null>(null);
  async function create(format: "epub" | "txt" | "pdf") {
    if (demoMode) { setMessage("示范模式不会生成文件。"); return; }
    try { await api.createExport(book.id, locale, format); await exports.refetch(); setMessage(`${format.toUpperCase()} 已加入生成队列。`); }
    catch (error) { setMessage(error instanceof Error ? error.message : "创建导出失败"); }
  }
  return (
    <div className="exports-layout">
      <header><span className="eyebrow">PUBLICATION</span><h2>生成出版文件</h2><p>三种格式从同一译稿版本生成，并各自记录哈希与验证结果。</p></header>
      <div className="export-options">
        <article><div><strong>EPUB</strong><span>目录、脚注、ruby 与插图位置</span></div><button className="button" type="button" onClick={() => void create("epub")}>生成 EPUB</button></article>
        <article><div><strong>TXT</strong><span>UTF-8 文本；报告列出省略的图片</span></div><button className="button" type="button" onClick={() => void create("txt")}>生成 TXT</button></article>
        <article><div><strong>PDF</strong><span>A5 横排、章节换页、目录与页码</span></div><button className="button" type="button" onClick={() => void create("pdf")}>生成 PDF</button></article>
      </div>
      {message && <p role="status" className="form-message">{message}</p>}
      <section className="artifact-list"><h3>生成记录</h3>{exports.error && <QueryState error={exports.error} label="导出记录" />}{(exports.data?.items.length ?? 0) === 0 ? <p className="muted-copy">当前版本还没有生成文件。</p> : exports.data?.items.map((item) => <article key={item.id}><div><strong>{item.format.toUpperCase()} · {localeName(item.locale)}</strong><span>{formatDate(item.created_at)}</span></div><StatusMark tone={item.status === "ready" ? "good" : item.status === "failed" ? "danger" : "neutral"}>{item.status === "ready" ? "验证通过" : item.status === "failed" ? "验证失败" : "生成中"}</StatusMark>{item.download_url ? <a className="button button--small" href={item.download_url}><Icons.download />下载</a> : <span className="muted-copy">{item.validation_message ?? "尚不可下载"}</span>}</article>)}</section>
    </div>
  );
}
