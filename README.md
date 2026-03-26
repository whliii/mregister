# mregister

`mregister` 是一个围绕临时邮箱服务和网页控制台组织起来的注册工具集。

仓库主要由三部分组成：

- `mail_adapter/`：临时邮箱服务，提供 GPTMail 兼容 API
- `openai-register/` 和 `grok-register/`：注册执行端
- `web_console/`：用于管理凭据、代理、模板和任务的网页控制台

## 架构关系

常见使用链路：

`mail_adapter` -> 邮箱 / 验证码接口 -> `openai-register` 或 `grok-register` -> `web_console` 统一调度

## 目录说明

- [mail_adapter/GMAIL_RELAY_README.md](mail_adapter/GMAIL_RELAY_README.md)：Gmail relay 临时邮箱服务说明
- [openai-register/README.md](openai-register/README.md)：OpenAI 注册端说明
- [grok-register/README.md](grok-register/README.md)：Grok 注册端说明(已废弃)
- [docker-compose.yml](docker-compose.yml)：根目录控制台服务编排

## 快速开始

先启动临时邮箱服务：

```bash
cd /home/ubuntu/mregister/mail_adapter
docker compose up -d --build
```

默认地址：

- GPTMail 兼容 API：`http://127.0.0.1:18787`
- 管理后台：`http://127.0.0.1:18787/admin/console`

再从仓库根目录启动网页控制台：

```bash
cd /home/ubuntu/mregister
docker compose up -d --build
```

控制台地址：

- `http://127.0.0.1:8000`

## 使用说明

- 根目录 `docker-compose.yml` 启动的是 `web_console`，不是 `mail_adapter`
- `mail_adapter/docker-compose.yml` 才是临时邮箱后台
- 注册执行端依赖可用的邮箱服务，没有邮箱服务时任务无法正常运行

## 本地敏感数据

这个仓库已经按公开代码仓库的方式处理过。

以下本地数据默认不会进入 Git：

- `.env` 文件
- SQLite 数据库
- 运行日志和任务输出
- 本地凭据与邮箱状态数据

示例环境变量可参考 [mail_adapter/.env.example](mail_adapter/.env.example)。
