# Gmail Relay GPTMail Adapter

把 Gmail 收件箱包装成 `GPTMail` 兼容 API，供当前仓库里的 `openai-register` 直接使用。

整体链路：

- 外部站点发邮件到 `random-local-part@your-domain`
- Cloudflare Email Routing 把整个域名的收件流转发到 Gmail
- 本服务通过 `IMAP` 拉 Gmail
- 识别原始收件地址里的完整目标邮箱
- 通过 GPTMail 兼容接口把邮件提供给 `openai-register`

## 功能

- GPTMail 兼容接口：
  - `GET /api/generate-email`
  - `POST /api/generate-email`
  - `GET /api/emails?email=...`
  - `GET /api/email/{id}`
  - `DELETE /api/email/{id}`
  - `DELETE /api/emails/clear?email=...`
  - `GET /api/domains`
- Gmail 收件箱管理
- 域名管理
- 内置管理后台：`/admin/console`
- Gmail 应用专用密码加密存储
- IMAP 后台轮询同步
- 没有待收验证码的临时邮箱时，自动跳过 Gmail 轮询

## 本地启动

```powershell
$env:ADAPTER_API_KEY="change-me"
$env:ADAPTER_ADMIN_TOKEN="change-admin-token"
$env:ADAPTER_SECRET_KEY="your-fernet-key-optional"
$env:ADAPTER_WEB_USERNAME="admin"
$env:ADAPTER_WEB_PASSWORD="change-this-password"
$env:ADAPTER_SYNC_INTERVAL_SECONDS="12"
$env:ADAPTER_MESSAGE_RETENTION_MINUTES="120"
$env:ADAPTER_MAILBOX_RETENTION_HOURS="24"
$env:ADAPTER_CLEANUP_INTERVAL_SECONDS="300"
python -m uvicorn mail_adapter.gmail_relay_gptmail_adapter:app --host 0.0.0.0 --port 8787
```

访问：

- 后台：`http://127.0.0.1:8787/admin/console`
- API：`http://127.0.0.1:8787`

## Docker 部署

在 `mail_adapter` 目录执行：

```powershell
$env:ADAPTER_API_KEY="change-me"
$env:ADAPTER_ADMIN_TOKEN="change-admin-token"
$env:ADAPTER_SECRET_KEY=""
$env:ADAPTER_WEB_USERNAME="admin"
$env:ADAPTER_WEB_PASSWORD="change-this-password"
$env:ADAPTER_SYNC_INTERVAL_SECONDS="12"
$env:ADAPTER_MESSAGE_RETENTION_MINUTES="120"
$env:ADAPTER_MAILBOX_RETENTION_HOURS="24"
$env:ADAPTER_CLEANUP_INTERVAL_SECONDS="300"
docker compose up -d --build
```

默认端口：

- GPTMail 兼容服务：`http://127.0.0.1:18787`

## 后台配置顺序

1. 先创建 `Gmail 收件箱`
2. 再创建 `域名`
3. 在 Cloudflare 里把整个域名的收件流转发到对应 Gmail
4. 在 `mregister` 控制台新增一个 `gptmail` 凭据，指向本服务

## Cloudflare 配置约定

每个域名需要让任意收件地址都能进入对应 Gmail 收件箱。

推荐做法：

- 为该域名启用 catch-all 或等效的全量转发规则
- 确保原始收件地址在邮件头中被保留

生成出来的地址会像这样：

- `abc123xyz0@mail.example.com`
- `k8m4wz2qtr@example.com`

## 在 mregister 里接入

新增 `gptmail` 凭据：

- `API Key` = `ADAPTER_API_KEY`
- `Base URL` = `http://你的服务器:18787`
- `domain` = 可留空；服务会在启用域名中轮询分配

## 管理接口

- `GET /api/admin/status`
- `GET /api/admin/gmail-inboxes`
- `POST /api/admin/gmail-inboxes`
- `PUT /api/admin/gmail-inboxes/{id}`
- `DELETE /api/admin/gmail-inboxes/{id}`
- `GET /api/admin/domains`
- `POST /api/admin/domains`
- `PUT /api/admin/domains/{id}`
- `DELETE /api/admin/domains/{id}`
- `POST /api/admin/sync`

自动清理默认值：
- 邮件保留 `120` 分钟
- 临时邮箱保留 `24` 小时
- 清理任务每 `300` 秒执行一次检查

请求头：

- `X-API-Key: <ADAPTER_API_KEY>`
- `X-Admin-Token: <ADAPTER_ADMIN_TOKEN>`

## 注意事项

- Gmail 需要先开启两步验证，再生成 `App Password`
- 该方案不再依赖自建 SMTP/MX，也不需要 Cloudflare Worker 回调你的服务器
- 数据默认保存在 `mail_adapter/data/adapter.db`
- 临时邮箱本地名随机生成；服务不再提供“默认域名”或“邮箱前缀”配置
