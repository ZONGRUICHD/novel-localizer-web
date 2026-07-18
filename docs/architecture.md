# 系统架构与安全边界

## 信任链

栞译台采用两层身份验证和源站隔离。浏览器首先通过 Cloudflare Access；Pages Function 验证前端 Access JWT 的签名、`iss`、`aud`、有效期与邮箱，再通过 Access `get-identity` 取得实际 IdP ID。随后它携带 Access Service Token 请求独立源站，把原始用户 JWT 放入 `Shiori-User-Assertion`，并在剥离浏览器同名头后写入 `Shiori-Verified-IdP`。FastAPI 会独立验证 Cloudflare 为源站 Access 应用签发的 `Cf-Access-Jwt-Assertion`、原始用户 JWT、所有者邮箱，以及内部 IdP 头是否精确命中 allowlist。

标准 Access app JWT 不保证包含 IdP claim，因此后端只在源站 service-token JWT 已通过后接受 BFF 写入的 `Shiori-Verified-IdP`。该头本身不是公共认证凭据，直接源站请求或浏览器伪造均不能绕过两枚 JWT 验签。后端从不信任浏览器提供的邮箱、`X-Forwarded-User` 或类似头。生产环境缺少任一 JWT、受信 IdP、Service Token、精确 Origin 或 CSRF token 时均 fail closed。API 不开放 CORS。

## 网络边界

- 前端：`translate.zongtech.xyz`，Cloudflare Pages。
- 源站：`translate-origin.zongtech.xyz`，仅通过独立 Cloudflare Tunnel。
- API：只监听 `127.0.0.1:18740`。
- 不新增 UFW 入站规则或路由器端口转发。
- 不修改 SSH 22/3199、HTTP 80/443、现有 11435 或既有 Tunnel/systemd 单元。
- Preview 没有生产源站 Service Token，任何 `/api/*` 请求都返回受控的 503。

## 密钥

- OpenAI-compatible API Key 使用 AES-256-GCM 加密后写入 SQLite；随机 96-bit nonce、版本化 AAD、密文和标签一起保存。
- 主密钥由 `LoadCredentialEncrypted=` 注入，服务通过 `$CREDENTIALS_DIRECTORY` 下的只读文件读取。
- 前端只接收 `configured`、尾号与最近验证时间。
- Provider Base URL 仅允许公网 HTTPS。每次连接前解析所有 A/AAAA，拒绝 loopback、RFC1918、链路本地、CGNAT、文档地址、多播和云元数据地址；HTTP 客户端禁用重定向并限制响应大小/超时。
- 日志过滤 Authorization、Cookie、Access JWT、API Key、书籍正文与 Prompt。

## 数据与任务

SQLite 使用 WAL。只有一个后台 Worker 领取任务租约；租约过期可恢复，章节级检查点保证服务重启不丢进度。人工锁定的 `SegmentRevision` 不会被重译覆盖，重译只增加候选版本。

`awaiting_review` 是不再被 Worker 领取的非终态；所有者确认后调用 resume 会保留 checkpoint 并重新排队。模型对人工锁定段落产生的新结果必须保存为 `SegmentRevision(revision_kind=model_candidate)`；编辑台与导出都优先使用人工版本，候选只用于比较和显式选择。

书籍、资料库与导出位于 `/var/lib/shiori`，配置位于 `/etc/shiori`，发布版本位于 `/opt/shiori/releases/<git-sha>`。原始书籍只读保存，解析和导出按版本另存。

## 内容处理

图片与保留封面不发送给模型。资料库只有在用户明确允许外部短片段处理时才能参与 Prompt；每批最多四个参考对、单侧 300 字、总参考 2400 字。`zh-CN` 和 `zh-TW` 直接从日文分别翻译，绝不以繁简转换代替台湾繁中翻译。

替换封面使用独立的 `purpose=cover` 分块上传，只接受扩展名、MIME 与文件魔数一致的 JPEG/PNG。封面上传完成时不会创建 `BookDocument`；项目选择“替换封面”时只能引用已完成的 cover upload。封面字节与内页图片一样不会发送给模型。
