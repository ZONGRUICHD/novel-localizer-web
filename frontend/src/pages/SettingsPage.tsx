import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useProvider, useUpdateProvider } from "../api/queries";
import { DemoFlag, QueryState, StatusMark } from "../components/Common";
import type { ProviderStatus } from "../types/api";
import { formatDate } from "../utils/format";

function capability(value: boolean | null): string {
  if (value === null) return "尚未探测";
  return value ? "可用" : "不可用";
}

export function SettingsPage({ demoMode }: { demoMode: boolean }) {
  const provider = useProvider(demoMode);
  const updateProvider = useUpdateProvider(demoMode);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [translationModel, setTranslationModel] = useState("");
  const [reviewModel, setReviewModel] = useState("");
  const [testResult, setTestResult] = useState<ProviderStatus | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  useEffect(() => {
    if (!provider.data) return;
    setBaseUrl(provider.data.base_url ?? "");
    setTranslationModel(provider.data.translation_model ?? "");
    setReviewModel(provider.data.review_model ?? "");
  }, [provider.data]);

  async function testConnection() {
    setTestError(null);
    setTestResult(null);
    if (demoMode) {
      setTestError("示范模式不会连接外部服务。");
      return;
    }
    try {
      setTestResult(await api.testProvider());
    } catch (error) {
      setTestError(error instanceof Error ? error.message : "连接测试失败");
    }
  }

  return (
    <section aria-labelledby="settings-title" className="page-section page-section--narrow">
      <header className="page-heading">
        <div>
          <span className="eyebrow">PREFERENCES</span>
          <h1 id="settings-title">设置</h1>
          <p>管理模型接口与阅读排版。完整密钥不会显示在页面中。</p>
        </div>
        {demoMode && <DemoFlag />}
      </header>

      {provider.isLoading && <div className="ruled-loader" role="status">正在读取模型接口设置…</div>}
      {provider.error && <QueryState error={provider.error} label="模型接口设置" />}
      {provider.data && (
        <form
          className="settings-form"
          onSubmit={(event) => {
            event.preventDefault();
            updateProvider.mutate({
              base_url: baseUrl,
              api_key: apiKey || undefined,
              translation_model: translationModel,
              review_model: reviewModel,
            });
          }}
        >
          <section className="settings-section" aria-labelledby="provider-heading">
            <header>
              <div><span className="section-number">01</span><h2 id="provider-heading">模型接口</h2></div>
              <StatusMark tone={provider.data.configured ? "good" : "warning"}>{provider.data.configured ? `已配置 ····${provider.data.key_suffix}` : "尚未配置"}</StatusMark>
            </header>
            <p className="settings-intro">只接受公网 HTTPS 地址。系统会拒绝重定向、本机、私网、链路本地与云元数据地址。</p>
            <div className="field-grid">
              <label className="field field--wide">
                <span>Base URL</span>
                <input aria-label="Base URL" type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} required placeholder="https://api.example.com/v1" autoComplete="url" />
                <small>首版调用 <code>/v1/chat/completions</code>。</small>
              </label>
              <label className="field field--wide">
                <span>API Key</span>
                <input aria-label="API Key" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} required={!provider.data.configured} placeholder={provider.data.configured ? "留空以保留当前密钥" : "输入密钥"} autoComplete="new-password" />
                <small>保存后仅显示尾号；浏览器不会再次取得完整密钥。</small>
              </label>
              <label className="field">
                <span>翻译模型</span>
                <input aria-label="翻译模型" value={translationModel} onChange={(event) => setTranslationModel(event.target.value)} required placeholder="model-name" spellCheck={false} />
              </label>
              <label className="field">
                <span>审校模型</span>
                <input aria-label="审校模型" value={reviewModel} onChange={(event) => setReviewModel(event.target.value)} required placeholder="model-name" spellCheck={false} />
              </label>
            </div>
            <div className="capability-table" aria-label="接口能力">
              <span>模型列表 <b>{capability(provider.data.capabilities.models_endpoint)}</b></span>
              <span>结构化结果 <b>{capability(provider.data.capabilities.json_response_format)}</b></span>
              <span>流式响应 <b>{capability(provider.data.capabilities.streaming)}</b></span>
              <span>最近验证 <b>{formatDate(provider.data.last_verified_at)}</b></span>
            </div>
            {(testError || updateProvider.error) && <p className="form-message form-message--error" role="alert">{testError ?? updateProvider.error?.message}</p>}
            {testResult && <p className="form-message" role="status">连接已验证，能力信息已更新。</p>}
            {updateProvider.isSuccess && <p className="form-message" role="status">设置已加密保存。</p>}
            <footer className="form-actions">
              <button className="button" type="button" onClick={() => void testConnection()}>连接测试</button>
              <button className="button button--primary" type="submit" disabled={updateProvider.isPending}>{updateProvider.isPending ? "保存中…" : "保存设置"}</button>
            </footer>
          </section>

          <section className="settings-section" aria-labelledby="security-heading">
            <header><div><span className="section-number">02</span><h2 id="security-heading">安全边界</h2></div></header>
            <dl className="security-list">
              <div><dt>访问身份</dt><dd>Cloudflare Access 所有者策略</dd></div>
              <div><dt>会话</dt><dd>8 小时，要求 MFA</dd></div>
              <div><dt>源站</dt><dd>经独立 Tunnel 与服务令牌访问</dd></div>
              <div><dt>跨域请求</dt><dd>关闭</dd></div>
            </dl>
          </section>
        </form>
      )}
    </section>
  );
}
