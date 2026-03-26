# Deprecated: OpenTrashmail GPTMail Adapter

This document is deprecated.

Use [GMAIL_RELAY_README.md](/home/ubuntu/mregister/mail_adapter/GMAIL_RELAY_README.md) for the current Gmail relay implementation.

把 OpenTrashmail 的接口转换为 `openai-register` 当前使用的 GPTMail API 形状。

## 1. 启动（本机 Python）

在项目根目录下执行：

```powershell
$env:ADAPTER_API_KEY="change-me"
$env:ADAPTER_ADMIN_TOKEN="change-admin-token"
$env:ADAPTER_INBOUND_TOKEN="change-inbound-token"
$env:ADAPTER_WEB_USERNAME="admin"
$env:ADAPTER_WEB_PASSWORD="change-this-password"
$env:OPENTRASHMAIL_BASE_URL="http://127.0.0.1:8080"
$env:OPENTRASHMAIL_PASSWORD=""   # 如 OpenTrashmail 配了 PASSWORD 则填这里
$env:ADAPTER_ALLOWED_DOMAINS="mail.example.com"
$env:ADAPTER_DEFAULT_DOMAIN="mail.example.com"
python -m uvicorn mail_adapter.opentrashmail_gptmail_adapter:app --host 0.0.0.0 --port 8787
```

## 2. 一键 Docker（推荐）

在 `mail_adapter` 目录下执行：

```powershell
$env:ADAPTER_API_KEY="change-me"
$env:ADAPTER_ADMIN_TOKEN="change-admin-token"
$env:ADAPTER_INBOUND_TOKEN="change-inbound-token"
$env:ADAPTER_WEB_USERNAME="admin"
$env:ADAPTER_WEB_PASSWORD="change-this-password"
$env:ADAPTER_ALLOWED_DOMAINS="mail.example.com"
$env:ADAPTER_DEFAULT_DOMAIN="mail.example.com"
docker compose up -d --build
```

启动后：

- OpenTrashmail UI/API: `http://127.0.0.1:18080`
- GPTMail 兼容适配器: `http://127.0.0.1:18787`
- 域名管理后台: `http://127.0.0.1:18787/admin/domains`

## 3. 提供的兼容接口

- `GET /api/generate-email`
- `POST /api/generate-email`
- `GET /api/emails?email=...`
- `GET /api/email/{id}`
- `DELETE /api/email/{id}`
- `DELETE /api/emails/clear?email=...`
- `GET /api/domains`
- `POST /api/inbound/cloudflare-email`

管理员接口：

- `GET /api/admin/domains`
- `POST /api/admin/domains`
- `PUT /api/admin/domains/{id}`
- `DELETE /api/admin/domains/{id}`

请求头：

- `X-API-Key: <ADAPTER_API_KEY>`
- `X-Admin-Token: <ADAPTER_ADMIN_TOKEN>`（管理员接口）
- `X-Inbound-Token: <ADAPTER_INBOUND_TOKEN>`（Cloudflare 入站接口）

网页登录：

- 用户名：`ADAPTER_WEB_USERNAME`
- 密码：`ADAPTER_WEB_PASSWORD`

默认网页账号：

- 用户名：`admin`
- 密码：`change-this-password`

Cloudflare Worker 示例：

- [cloudflare_email_worker_example.mjs](/home/ubuntu/mregister/mail_adapter/cloudflare_email_worker_example.mjs)

## 4. 在 mregister 里接入

在 Web 控制台新增 `gptmail` 凭据：

- `API Key` = `ADAPTER_API_KEY`
- `Base URL` = `http://你的适配器地址:18787`
- `domain` = 你的域名（可选）

然后创建 `openai-register` 任务即可。

## 5. 说明

- 该适配器把 OpenTrashmail 的 `email + mail_id` 编码成单个 `id`，用于兼容 GPTMail 客户端的 `GET /api/email/{id}` 调用模型。
- 仅做协议适配，不负责 SMTP/MX 基础设施。
- 域名池保存在 `mail_adapter/data/adapter_domains.db`，重启后仍会保留。
