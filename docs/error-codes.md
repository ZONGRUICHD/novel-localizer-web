# 稳定错误码

API 错误统一返回 `{"error":{"code":"…","message":"…","details":{…}}}`，HTTP 状态表达协议层结果，`code` 用于界面和重试决策。

| Code | 意义 | 客户端动作 |
| --- | --- | --- |
| `UNSUPPORTED_DRM` | EPUB/PDF 含不支持的加密 | 停止；提供无 DRM 原版 |
| `OCR_REQUIRED` | PDF 无可靠文字层 | 停止；首版不提供 OCR |
| `ENCODING_CONFIRMATION_REQUIRED` | TXT 编码置信不足 | 用户确认编码后继续 |
| `ALIGNMENT_REVIEW_REQUIRED` | 日中配对存在低置信对齐 | 人工确认窗口 |
| `API_INCOMPATIBLE` | Provider 不满足 Chat Completions 协议 | 修改 Base URL/模型 |
| `RATE_LIMITED` | Provider 429 或本地并发限制 | 按服务端退避时间重试 |
| `EXPORT_VALIDATION_FAILED` | EPUB/TXT/PDF 结构或渲染验证失败 | 保留任务并进入处理状态 |
| `INVALID_ACCESS_TOKEN` | JWT 缺失、篡改、过期或声明不符 | 重新认证；不重放写请求 |
| `CSRF_FAILED` | Origin 或 CSRF token 不匹配 | 重新取得 session token |
| `UPLOAD_HASH_MISMATCH` | 分块或完成哈希不符 | 重传受影响分块 |
| `INVALID_COVER` | 替换封面用途、格式、MIME、魔数或状态不合法 | 重新上传 JPEG/PNG 封面并等待完成 |
| `PROVIDER_URL_BLOCKED` | Provider 地址可能访问私网/元数据 | 改用公网 HTTPS 地址 |

错误消息不得包含 Provider Key、JWT、书籍正文、Prompt、服务器路径或数据库 SQL。
