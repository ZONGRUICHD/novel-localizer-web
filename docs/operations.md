# 运维与恢复

## 日常检查

```sh
systemctl --no-pager --full status shiori-api shiori-worker cloudflared-shiori
journalctl -u shiori-api -u shiori-worker --since today --no-pager
systemctl list-timers shiori-backup.timer shiori-cleanup.timer
```

健康检查走受保护的同源 `/api/session`；本机可检查 `http://127.0.0.1:18740/healthz`，它只返回进程/DB 状态，不包含身份或正文。

## 备份

`shiori-backup.timer` 每日调用 SQLite Online Backup API 生成一致快照，然后用 restic 上传数据库快照、原书、检查点与导出。策略为 7 日、4 周、12 月。主加密密钥不进入 restic；灾难恢复后重新填写 Provider API Key。

每季度在隔离目录运行 `restore-drill.sh`，验证 `PRAGMA integrity_check`、文件哈希和至少一个导出下载，不连接生产服务。

## 回滚

列出 `/opt/shiori/releases`，将 `current` 原子链接切回上一 SHA，重启 API/Worker，并验证 DB schema 兼容。数据库迁移只允许可向后兼容的 expand/contract；禁止在同一次发布中删除旧列。

## 数据保留

原始文件、人工译文与最终导出默认永久保留，临时 chunk、失败解析目录和未引用缓存由 cleanup timer 清理。取消任务保留已完成章节。删除项目属于单独的、需二次确认的恢复性操作，不由定时器隐式执行。
