import { useEffect, useState } from "react";
import { useSession } from "./api/queries";
import { QueryState, StatusMark } from "./components/Common";
import { Icons } from "./components/Icons";
import { BookWorkspace } from "./pages/BookWorkspace";
import { BooksPage } from "./pages/BooksPage";
import { JobsPage } from "./pages/JobsPage";
import { LibrariesPage } from "./pages/LibrariesPage";
import { SettingsPage } from "./pages/SettingsPage";
import type { BookSummary } from "./types/api";

type MainView = "books" | "libraries" | "jobs" | "settings";

const navigation: Array<{ id: MainView; label: string; icon: typeof Icons.book }> = [
  { id: "books", label: "书库", icon: Icons.book },
  { id: "libraries", label: "资料库", icon: Icons.archive },
  { id: "jobs", label: "任务", icon: Icons.task },
  { id: "settings", label: "设置", icon: Icons.settings },
];

export default function App({ demoMode = false }: { demoMode?: boolean }) {
  const [view, setView] = useState<MainView>("books");
  const [openBook, setOpenBook] = useState<BookSummary | null>(null);
  const [fontSize, setFontSize] = useState(17);
  const [lineHeight, setLineHeight] = useState(1.85);
  const [columnWidth, setColumnWidth] = useState(34);
  const session = useSession(demoMode);

  useEffect(() => {
    document.documentElement.style.setProperty("--reader-font-size", `${fontSize}px`);
    document.documentElement.style.setProperty("--reader-line-height", String(lineHeight));
    document.documentElement.style.setProperty("--reader-column-width", `${columnWidth}rem`);
  }, [fontSize, lineHeight, columnWidth]);

  function navigate(next: MainView) {
    setOpenBook(null);
    setView(next);
    window.requestAnimationFrame(() => document.getElementById("main-content")?.focus());
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="site-header">
        <button className="brand" type="button" onClick={() => navigate("books")} aria-label="栞译台首页">
          <span className="brand-stamp" aria-hidden="true">栞</span>
          <span><strong>栞译台</strong><small>SHIORI</small></span>
        </button>
        <nav aria-label="主导航" className="main-navigation">
          {navigation.map((item) => {
            const Icon = item.icon;
            const active = !openBook && view === item.id;
            return <button key={item.id} type="button" className={active ? "active" : ""} aria-current={active ? "page" : undefined} onClick={() => navigate(item.id)}><Icon />{item.label}</button>;
          })}
        </nav>
        <div className="header-tools">
          <details className="reading-menu">
            <summary aria-label="阅读排版设置"><span aria-hidden="true">文</span></summary>
            <div className="reading-menu__panel">
              <header><strong>阅读排版</strong><span>仅保存在本次浏览中</span></header>
              <label><span>字号 <output>{fontSize}px</output></span><input type="range" min="14" max="24" step="1" value={fontSize} onChange={(event) => setFontSize(Number(event.target.value))} /></label>
              <label><span>行高 <output>{lineHeight.toFixed(2)}</output></span><input type="range" min="1.45" max="2.25" step="0.05" value={lineHeight} onChange={(event) => setLineHeight(Number(event.target.value))} /></label>
              <label><span>栏宽 <output>{columnWidth}rem</output></span><input type="range" min="25" max="48" step="1" value={columnWidth} onChange={(event) => setColumnWidth(Number(event.target.value))} /></label>
            </div>
          </details>
          <div className="owner-state">
            {session.isLoading && <span>验证会话…</span>}
            {session.data && <><StatusMark tone={session.data.service.status === "ready" ? "good" : "warning"}>{session.data.service.status === "ready" ? "服务就绪" : "服务受限"}</StatusMark><span className="owner-initial" aria-hidden="true">{session.data.owner.display_name?.slice(0, 1).toUpperCase() ?? "主"}</span><span className="visually-hidden">已登录：{session.data.owner.email}</span></>}
          </div>
        </div>
      </header>

      <nav className="mobile-navigation" aria-label="移动端主导航">
        {navigation.map((item) => {
          const Icon = item.icon;
          const active = !openBook && view === item.id;
          return <button key={item.id} type="button" className={active ? "active" : ""} aria-current={active ? "page" : undefined} onClick={() => navigate(item.id)}><Icon /><span>{item.label}</span></button>;
        })}
      </nav>

      <main id="main-content" tabIndex={-1}>
        {session.error && !demoMode ? <div className="session-failure"><QueryState error={session.error} label="会话" /></div> : (
          openBook ? <BookWorkspace book={openBook} demoMode={demoMode} onBack={() => setOpenBook(null)} /> : (
            <>
              {view === "books" && <BooksPage demoMode={demoMode} onOpen={setOpenBook} />}
              {view === "libraries" && <LibrariesPage demoMode={demoMode} />}
              {view === "jobs" && <JobsPage demoMode={demoMode} />}
              {view === "settings" && <SettingsPage demoMode={demoMode} />}
            </>
          )
        )}
      </main>
    </div>
  );
}
