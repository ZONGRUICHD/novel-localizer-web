# 生产部署手册

## 0. 停止条件

先轮换对话中暴露过的 SSH/sudo 密码并完成 SSH Key 登录。不要把旧密码写入命令、Actions Secret 或配置。运行 `deploy/scripts/preflight.sh` 进行只读检查；出现下列任一情况立即停止：

- `127.0.0.1:18740` 已被其他进程监听；
- `/var/lib/shiori` 所在文件系统可用空间不足 10 GiB；
- 系统不是受支持的 systemd Linux；
- 新配置会改动现有 11435、SSH、UFW 入站规则或既有 Tunnel。

## 1. Cloudflare

1. 创建 Pages 项目并连接私有 GitHub 仓库，生产分支 `main`，根目录 `frontend`，构建命令 `pnpm install --frozen-lockfile && pnpm build`，输出 `dist`。
2. 为 `translate.zongtech.xyz`、生产 `*.pages.dev` 建 Access 应用；在 Pages 设置中单独开启 Preview Access policy。
3. 创建 Google 与 GitHub IdP，分别验证其返回邮箱。建立两条精确 Allow：登录方法 + `zongrui0831@outlook.com`；要求 MFA，8 小时会话。不得启用 Everyone、域通配或 OTP。
4. 创建独立源站 Access 应用与 Service Auth policy。在生产 Pages encrypted secrets 设置 `ACCESS_TEAM_DOMAIN`、`ACCESS_AUD`、`OWNER_EMAIL`、`ACCESS_ALLOWED_IDP_IDS`、`PUBLIC_ORIGIN`、`SHIORI_ORIGIN`、`CF_ACCESS_CLIENT_ID`、`CF_ACCESS_CLIENT_SECRET`、`CSRF_SHARED_SECRET` 和 `SHIORI_ENVIRONMENT=production`。Preview 不设置任何生产源站 Service Token 或 BFF 密钥，`/api/*` 必须 fail closed。
5. 创建独立 Tunnel `shiori-origin`，唯一 public hostname 为 `translate-origin.zongtech.xyz → http://127.0.0.1:18740`。不要复用现有 Tunnel 路由。
6. 创建私有 R2 bucket `shiori-restic-backup` 和仅对该 bucket 有权的 S3 API token。

## 2. 主机

预检通过后，可以审阅并运行范围受限的 bootstrap。它只创建 Shiori 用户、目录、单元与发布入口，不启动服务，也不修改 SSH、UFW、DNS、11435 或既有 Tunnel：

```sh
sudo env SHIORI_BOOTSTRAP_CONFIRM=INSTALL_SHIORI_ONLY deploy/scripts/bootstrap-host.sh
```

使用 `systemd-creds encrypt` 分别生成 API Key 主密钥、CSRF secret、Tunnel token 与 restic 凭据；不要把明文写入 EnvironmentFile。主密钥输入应为 32 字节随机值的 base64 文本，CSRF secret 至少 32 字节。配置文件还必须填入两个不同的 Access AUD 和两个实际 IdP UUID。Pages BFF 必须用 `get-identity` 确认 IdP、删除浏览器传入的 `Shiori-Verified-IdP`，再写入服务端确认值；源站 allowlist 使用同一组实际 UUID。安装 `deploy/systemd/` 下的单元，执行 daemon-reload 后启动 API、Worker 与 Tunnel，最后启用 timer。

创建仅用于 GitHub 发布的 `shiori-deploy` 账户和 SSH deploy key。该账户不能拥有 sudo 以外的管理员权限；bootstrap 会安装只允许执行 `shiori-install-release <sha>` 的 sudoers 规则。发布安装器用 `shiori` 身份创建虚拟环境和安装 wheel，随后把 release 改为 root 所有，避免把来自暂存目录的 Python 代码作为 root 执行。

## 3. 版本化发布

后端 Actions 只上传已测试的归档到固定暂存目录；根权限安装器只接受 SHA，并且只读取该目录下的一份归档和 wheel。它不以 root 执行应用迁移；API 的 `ExecStartPre` 作为受限 `shiori` 用户使用 systemd credentials 执行迁移。随后才原子切换 `/opt/shiori/current`。服务启动失败时链接自动回滚到上一版本。发布 Environment 必须人工批准，使用 `shiori-deploy` 专用 SSH deploy key、固定 host key 和受限 sudoers 规则。

## 4. 验收

- 两个所有者 IdP 均可登录，其他账号拒绝。
- 篡改/过期 JWT、缺 Service Token、直接源站均拒绝。
- `ss -ltnp` 只显示 API 在 `127.0.0.1:18740`。
- 现有 11435 服务与公网检查保持原状态。
- API Key 不在响应、浏览器存储、日志、Git 历史或 Pages bundle。
- 完成合成 EPUB/TXT/PDF 全流程与至少一个真实章节盲审。
- 隔离恢复最近 restic 快照并通过 SQLite integrity check。
