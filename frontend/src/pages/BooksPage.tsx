import { useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useBooks } from "../api/queries";
import { DemoFlag, EmptyState, QueryState, StatusMark } from "../components/Common";
import { Icons } from "../components/Icons";
import type { BookSummary } from "../types/api";
import { formatDate, localeName } from "../utils/format";

const stateLabel: Record<BookSummary["state"], string> = {
  imported: "已导入",
  parsing: "解析结构",
  ready: "可开始",
  translating: "翻译",
  review: "待处理问题",
  completed: "已完成",
  failed: "处理失败",
};

function stateTone(state: BookSummary["state"]): "good" | "warning" | "danger" | "neutral" {
  if (state === "completed" || state === "ready") return "good";
  if (state === "review") return "warning";
  if (state === "failed") return "danger";
  return "neutral";
}

export function BooksPage({ demoMode, onOpen }: { demoMode: boolean; onOpen: (book: BookSummary) => void }) {
  const books = useBooks(demoMode);
  const [search, setSearch] = useState("");
  const [uploadState, setUploadState] = useState<{ progress: number; label: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const visibleBooks = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    return (books.data?.items ?? []).filter((book) =>
      !term || `${book.title} ${book.author ?? ""}`.toLocaleLowerCase().includes(term),
    );
  }, [books.data, search]);

  async function upload(file: File | undefined) {
    if (!file) return;
    if (demoMode) {
      setUploadState({ progress: 0, label: "示范模式不会读取文件" });
      return;
    }
    setUploadState({ progress: 0, label: `正在上传 ${file.name}` });
    try {
      await api.uploadBook(file, (progress) => setUploadState({ progress, label: `正在上传 ${file.name}` }));
      setUploadState({ progress: 1, label: "上传完成，等待解析" });
      await books.refetch();
    } catch (error) {
      setUploadState({ progress: 0, label: error instanceof Error ? error.message : "上传失败" });
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <section aria-labelledby="books-title" className="page-section">
      <header className="page-heading">
        <div>
          <span className="eyebrow">COLLECTION</span>
          <h1 id="books-title">书库</h1>
          <p>原稿、译稿和出版成品都从这里进入。</p>
        </div>
        <div className="heading-actions">
          {demoMode && <DemoFlag />}
          <input
            ref={fileInput}
            className="visually-hidden"
            type="file"
            accept=".epub,.txt,.pdf,application/epub+zip,text/plain,application/pdf"
            onChange={(event) => void upload(event.target.files?.[0])}
            id="book-upload"
          />
          <button className="button button--primary" type="button" onClick={() => fileInput.current?.click()}>
            <Icons.upload />导入书稿
          </button>
        </div>
      </header>

      {uploadState && (
        <div className="upload-notice" role="status">
          <span>{uploadState.label}</span>
          <progress value={uploadState.progress} max="1"><span>{Math.round(uploadState.progress * 100)}%</span></progress>
        </div>
      )}

      <div className="list-toolbar">
        <label className="search-field">
          <span className="visually-hidden">搜索书名或作者</span>
          <Icons.search />
          <input value={search} onChange={(event) => setSearch(event.target.value)} type="search" placeholder="搜索书名或作者" />
        </label>
        <p aria-live="polite">{search ? `找到 ${visibleBooks.length} 项` : "按最近修改排序"}</p>
      </div>

      {books.isLoading && <div className="ruled-loader" role="status">正在读取书目…</div>}
      {books.error && <QueryState error={books.error} label="书库" />}
      {!books.isLoading && !books.error && visibleBooks.length === 0 && (
        <EmptyState title={search ? "没有匹配的书稿" : "书架还是空的"}>
          {search ? "换一个关键词，或清除搜索条件。" : "导入 EPUB、TXT 或带文字层的 PDF，建立第一本翻译项目。"}
        </EmptyState>
      )}

      {visibleBooks.length > 0 && (
        <div className="book-list" role="list" aria-label="书稿列表">
          <div className="book-list__head" aria-hidden="true">
            <span>书名</span><span>目标版本</span><span>状态</span><span>最近修改</span><span />
          </div>
          {visibleBooks.map((book) => (
            <article className="book-row" role="listitem" key={book.id}>
              <button className="book-row__open" type="button" onClick={() => onOpen(book)} aria-label={`打开《${book.title}》`}>
                <span className={`book-spine book-spine--${book.source_format}`} aria-hidden="true">{book.source_format.toUpperCase()}</span>
                <span className="book-title">
                  <strong lang={book.source_language}>{book.title}</strong>
                  <small>{book.author ?? "作者信息未填写"} · {book.section_count === null ? "章节数待解析" : `${book.section_count} 章`}</small>
                </span>
              </button>
              <span className="book-locales">{book.target_locales.length ? book.target_locales.map(localeName).join(" · ") : "尚未选择"}</span>
              <StatusMark tone={stateTone(book.state)}>{stateLabel[book.state]}</StatusMark>
              <time dateTime={book.updated_at}>{formatDate(book.updated_at)}</time>
              <button className="icon-button" type="button" aria-label={`进入《${book.title}》编辑台`} onClick={() => onOpen(book)}><Icons.chevron /></button>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
