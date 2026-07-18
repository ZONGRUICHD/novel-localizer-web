# 栞译台 Shiori

栞译台是一个单所有者、私有部署的日文书籍翻译与出版编辑台。它把 EPUB、TXT 和带可靠文字层的 PDF 解析为稳定的文档结构，分别生成 `zh-CN` 或台湾繁中 `zh-TW`，提供逐段编辑、人工锁定、审校与 EPUB/TXT/PDF 导出。

本项目不是 Codex Skill、Agent 或聊天机器人。仓库不会包含 `SKILL.md`、Agent 工作流、用户书籍或私有语料；模型只通过后端调用用户配置的 OpenAI-compatible Chat Completions API。

## 当前能力

- EPUB 按 OPF spine 读取，识别封面、ruby、脚注、内部链接和插图；安全拒绝未知加密与危险 ZIP。
- TXT 支持 UTF-8、BOM、CP932/Shift-JIS，并在编码置信不足时暂停确认。
- PDF 分类横排文字层、竖排文字层与扫描/坏文字层；扫描件返回 `OCR_REQUIRED`，首版不做 OCR。
- 统一 `BookDocument → Section → Block → Inline/Asset` 模型与稳定 Block ID。
- 资料库权限声明、内容哈希去重、SQLite FTS5 bigram/trigram 检索、术语/角色/风格画像。
- `zh-CN` 与 `zh-TW` 独立配置；默认翻译与审校两遍，支持断点、暂停、继续、取消和人工锁定。
- 任意输入可导出 EPUB、UTF-8 TXT 与横排 PDF；封面可保留、替换或移除。
- Cloudflare Access 双重验证、Pages 同源 BFF、API Key AES-256-GCM 加密和 SSRF 防护。

## 架构

```text
浏览器
  → Cloudflare Access（GitHub / Google，仅所有者）
  → Cloudflare Pages（React/Vite + /api Pages Function）
  → Access Service Token + 原始用户 JWT
  → translate-origin.zongtech.xyz
  → 独立 Cloudflare Tunnel
  → 127.0.0.1:18740 FastAPI
  → SQLite WAL + 单 Worker + 本地文件存储
  → OpenAI-compatible /v1/chat/completions
```

详细边界见 [系统架构](docs/architecture.md)、[部署手册](docs/deployment.md)、[语料与版权规则](docs/corpus-policy.md) 和 [运维手册](docs/operations.md)。

## 本地开发

### 后端

```powershell
cd backend
uv sync --all-extras
$env:PYTHONPATH = "$PWD\src" # Windows 下工作目录含中文时，确保 Python 直接定位源码
$env:SHIORI_ENV = "test"
uv run alembic upgrade head
uv run uvicorn shiori.main:app --host 127.0.0.1 --port 18740
```

另开终端运行持久 Worker：

```powershell
cd backend
uv run python -m shiori.worker
```

### 前端

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

前端静态构建与 Pages Function 本地联调：

```powershell
cd frontend
pnpm build
pnpm exec wrangler pages dev dist
```

本地开发必须显式使用测试认证开关，生产配置没有认证旁路。任何 API Key 都只写入后端测试数据目录；不要放入 `.env`、浏览器存储或命令历史。

## 验证

```powershell
cd backend
uv run pytest
uv run ruff check .
uv run mypy src

cd ..\frontend
pnpm test
pnpm lint
pnpm build
pnpm exec wrangler pages functions build
```

测试夹具全部自造。`C:\Users\zongrui\Documents\败犬女主太多了` 与 `lightnovel-2025` 只可用于本地、私有验收，不会复制到本仓库。

## 生产发布前的硬门槛

1. 立即更换曾在对话中暴露的 SSH 与 sudo 密码，并配置专用 SSH deploy key。项目和自动化不会使用旧密码。
2. 运行只读预检；若 `127.0.0.1:18740` 已占用或可用磁盘少于 10 GiB，停止部署。
3. 在 Cloudflare 中完成两个 IdP 的实际邮箱返回测试，精确放行 `zongrui0831@outlook.com`，要求 MFA、8 小时会话。
4. 同时保护自定义域、生产 `pages.dev` 与所有 Preview；Preview 不设置生产源站 Service Token。
5. 提供至少一卷与现有中文译本对应、无 DRM 的日文原版，并逐资料库确认私人处理和外部短片段权限。
6. 创建 systemd encrypted credential、独立 Tunnel 和私有 R2 restic bucket 后，才运行后端发布工作流。

代码按私有专有项目管理。未经所有者书面许可，不得复制、分发或公开部署。
