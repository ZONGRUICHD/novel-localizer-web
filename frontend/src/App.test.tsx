import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><App demoMode /></QueryClientProvider>);
}

describe("Shiori application shell", () => {
  it("exposes the four restrained primary destinations", async () => {
    renderApp();
    expect(await screen.findByRole("heading", { name: "书库" })).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    expect(navigation).toHaveTextContent("书库资料库任务设置");
    expect(screen.queryByText(/AI 正在思考|聊天|机器人/)).not.toBeInTheDocument();
  });

  it("opens a book into the keyboard-addressable translation editor", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "打开《試作短編集 — 縦書き検証版》" }));
    expect(screen.getByRole("tab", { name: "翻译" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("main", { name: "日中逐段对照" })).toBeInTheDocument();
    expect(screen.getByLabelText("第 1 段简体中文译文")).toHaveAttribute("lang", "zh-CN");
    expect(document.querySelectorAll('[lang="ja"]').length).toBeGreaterThan(0);
  });

  it("navigates to provider settings without displaying a stored key", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("navigation", { name: "主导航" }).querySelectorAll("button")[3]);
    expect(await screen.findByRole("heading", { name: "模型接口" })).toBeInTheDocument();
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("API Key")).toHaveValue("");
  });
});
