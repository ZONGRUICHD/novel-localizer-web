import { useLibraries } from "../api/queries";
import { DemoFlag, EmptyState, QueryState, StatusMark } from "../components/Common";
import { localeName } from "../utils/format";

export function LibrariesPage({ demoMode }: { demoMode: boolean }) {
  const libraries = useLibraries(demoMode);
  return (
    <section aria-labelledby="libraries-title" className="page-section">
      <header className="page-heading">
        <div>
          <span className="eyebrow">REFERENCE</span>
          <h1 id="libraries-title">资料库</h1>
          <p>管理对齐译本、术语记忆和作品风格，不训练模型。</p>
        </div>
        <div className="heading-actions">
          {demoMode && <DemoFlag />}
          <button className="button button--primary" type="button">新建资料库</button>
        </div>
      </header>

      <aside className="editorial-note" aria-label="外部处理提示">
        <strong>发送边界</strong>
        <p>只有明确允许外部处理的资料库，才会向配置的模型接口发送短参考片段；封面与内页图片从不发送。</p>
      </aside>

      {libraries.isLoading && <div className="ruled-loader" role="status">正在读取资料库…</div>}
      {libraries.error && <QueryState error={libraries.error} label="资料库" />}
      {!libraries.isLoading && !libraries.error && (libraries.data?.items.length ?? 0) === 0 && (
        <EmptyState title="还没有资料库">建立单语风格资料库，或配对同卷日文原版与既有中文译本。</EmptyState>
      )}
      {(libraries.data?.items.length ?? 0) > 0 && (
        <div className="library-list" role="list" aria-label="资料库列表">
          <div className="library-list__head" aria-hidden="true">
            <span>资料库</span><span>方式</span><span>目标地区</span><span>优先级</span><span>外部短片段</span>
          </div>
          {libraries.data?.items.map((library) => (
            <article className="library-row" role="listitem" key={library.id}>
              <div>
                <strong>{library.name}</strong>
                <p>{library.note ?? "未填写备注"}</p>
              </div>
              <span>{library.mode === "paired" ? "日中配对" : "单语风格"}<small>{library.source_count} 个来源</small></span>
              <span>{localeName(library.target_locale)}</span>
              <span className="priority-number">{library.priority}</span>
              <StatusMark tone={library.external_processing_allowed ? "good" : "neutral"}>
                {library.external_processing_allowed ? "已允许" : "不允许"}
              </StatusMark>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
