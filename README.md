# mregister

`mregister` is a registration toolkit built around a temporary mailbox service and a web console.

It consists of three main parts:

- `mail_adapter/`: temporary mailbox service with a GPTMail-compatible API
- `openai-register/` and `grok-register/`: registration workers
- `web_console/`: web UI for credentials, proxies, templates, and task execution

## Architecture

Typical flow:

`mail_adapter` -> mailbox / OTP API -> `openai-register` or `grok-register` -> `web_console` for orchestration

## Repository Layout

- [mail_adapter/GMAIL_RELAY_README.md](mail_adapter/GMAIL_RELAY_README.md): Gmail relay mailbox service
- [openai-register/README.md](openai-register/README.md): OpenAI registration worker
- [grok-register/README.md](grok-register/README.md): Grok registration worker
- [docker-compose.yml](docker-compose.yml): web console stack

## Quick Start

Start the mailbox service first:

```bash
cd /home/ubuntu/mregister/mail_adapter
docker compose up -d --build
```

Default endpoints:

- GPTMail-compatible API: `http://127.0.0.1:18787`
- Admin console: `http://127.0.0.1:18787/admin/console`

Then start the web console from the repository root:

```bash
cd /home/ubuntu/mregister
docker compose up -d --build
```

Web console:

- `http://127.0.0.1:8000`

## Notes

- Root `docker-compose.yml` starts `web_console`, not `mail_adapter`
- `mail_adapter/docker-compose.yml` starts the mailbox backend
- The registration workers depend on a working mailbox service

## Local Secrets

This repository is prepared for public source control.

The following local data is intentionally excluded from Git:

- `.env` files
- SQLite databases
- runtime logs and task output
- local credentials and mailbox state

Examples are provided via environment templates such as [mail_adapter/.env.example](mail_adapter/.env.example).
