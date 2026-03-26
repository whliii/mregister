# mregister 使用提醒

这个项目不是单独的“一个程序”，而是几部分配合使用：

1. `mail_adapter/`
临时邮箱服务。负责生成邮箱地址、收验证码、对外提供 GPTMail 兼容 API。

2. `openai-register/` 和 `grok-register/`
真正的注册脚本。它们调用临时邮箱服务拿邮箱和验证码，再去完成注册。

3. `web_console/`
统一控制台。用网页方式管理凭据、任务、并发和结果。

## 先理解关系

最常见链路是：

`mail_adapter` -> 提供临时邮箱 API -> `openai-register` / `grok-register` 使用 -> `web_console` 统一调度

也就是说：

- 临时邮箱和注册端是两套东西，不冲突
- 临时邮箱是基础设施
- 注册端是业务执行器
- 控制台是管理层，不是必须，但推荐用

## 目录分别做什么

### `mail_adapter/`

当前建议看这个文档：

- [mail_adapter/GMAIL_RELAY_README.md](/home/ubuntu/mregister/mail_adapter/GMAIL_RELAY_README.md)

作用：

- 生成临时邮箱
- 通过 Gmail + Cloudflare Email Routing 收验证码
- 提供 `GPTMail` 兼容接口给注册脚本或控制台使用

默认数据目录：

- `mail_adapter/data/`

本地敏感文件：

- `mail_adapter/.env`
- `mail_adapter/data/*.db`

这些已经被 Git 忽略，不会上传。

### `openai-register/`

作用：

- 调用临时邮箱
- 自动注册 OpenAI
- 输出 token 和账号结果

说明文档：

- [openai-register/README.md](/home/ubuntu/mregister/openai-register/README.md)

### `grok-register/`

作用：

- 调用临时邮箱
- 配合 YesCaptcha 自动注册 Grok / x.ai

说明文档：

- [grok-register/README.md](/home/ubuntu/mregister/grok-register/README.md)

### `web_console/`

作用：

- 用网页统一管理 `GPTMail`、`YesCaptcha`、代理、任务模板和执行结果
- 支持调度 `openai-register` 和 `grok-register`

根目录这个 `docker-compose.yml` 启动的是它，不是邮箱服务：

- [docker-compose.yml](/home/ubuntu/mregister/docker-compose.yml)

## 正常使用顺序

推荐顺序如下：

1. 先启动 `mail_adapter`
2. 确认临时邮箱 API 可用
3. 再启动 `web_console`
4. 在控制台里添加邮箱凭据
5. 最后创建注册任务

## 最常用启动方式

### 方式一：先启动临时邮箱服务

在 `mail_adapter/` 下准备好 `.env` 后执行：

```bash
cd /home/ubuntu/mregister/mail_adapter
docker compose up -d --build
```

默认访问：

- GPTMail API: `http://127.0.0.1:18787`
- 管理后台: `http://127.0.0.1:18787/admin/console`

如果你还没配 Gmail / Cloudflare，这一步先不要往后做任务，不然只会生成邮箱但收不到验证码。

### 方式二：启动控制台

在项目根目录执行：

```bash
cd /home/ubuntu/mregister
docker compose up -d --build
```

默认访问：

- 控制台: `http://127.0.0.1:8000`

## 控制台里怎么填

如果 `web_console` 是用根目录 Docker 启动的，而 `mail_adapter` 也是跑在本机 Docker 上，建议在控制台新增 `gptmail` 凭据时这样填：

- `kind`: `gptmail`
- `API Key`: 你的 `ADAPTER_API_KEY`
- `Base URL`: `http://host.docker.internal:18787`
- `domain`: 可留空，或者填你已启用的域名

如果控制台不是跑在 Docker 里，而是直接本机运行，`Base URL` 才用：

- `http://127.0.0.1:18787`

## 如果你不用控制台

也可以直接单独跑注册脚本：

### OpenAI

```bash
cd /home/ubuntu/mregister/openai-register
python ncs_register.py --non-interactive --email-credentials-file /path/to/email_credentials.json
```

前提：

- 临时邮箱服务已经可用
- 相关 `GPTMAIL_API_KEY`、`GPTMAIL_BASE_URL` 已配置

### Grok

```bash
cd /home/ubuntu/mregister/grok-register
python grok.py
```

前提：

- 已配置 `.env` 里的 `YESCAPTCHA_KEY`
- 临时邮箱服务可用

## 你现在最应该记住的

- 根目录 `docker-compose.yml` 只启动控制台，不启动临时邮箱
- `mail_adapter/docker-compose.yml` 才是临时邮箱服务
- 临时邮箱是前置条件，注册端依赖它
- 没有可用邮箱服务时，注册脚本基本跑不通
- 数据库和 `.env` 已经被 Git 忽略，本地能正常用，推送远程也不会带上

## Git 上传提醒

当前已经忽略这些本地文件：

- `.env`
- `*.db`
- `web_console/runtime/`
- `mail_adapter/data/`
- 日志和临时输出

所以可以正常上传代码，不会把你现在的配置和数据库一起传上去。
