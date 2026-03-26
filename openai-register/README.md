# OpenAI 自动注册脚本使用教程

## 环境准备
1. 创建并激活虚拟环境（Python 3.9+）：
```bash
cd openai-register
python3 -m venv venv
source venv/bin/activate    # Windows 用 venv\Scripts\activate
```

2. 安装依赖：
```bash
pip install curl_cffi requests
```

3. 配置 GPTMail（必需）：运行前设置环境变量
```
GPTMAIL_API_KEY=你的_key
# 可选：GPTMAIL_BASE_URL、GPTMAIL_TIMEOUT、GPTMAIL_PREFIX、GPTMAIL_DOMAIN
```

## 运行
```bash
cd openai-register
source venv/bin/activate
# 如需代理：--proxy http://127.0.0.1:7890
python ncs_register.py --non-interactive --email-credentials-file /path/to/email_credentials.json
```

参数说明：
- `--proxy` 可选，HTTP/S 代理地址
- `--once` 只运行一次（默认循环模式）

## 输出位置
- tokens：`openai-register/tokens/token_<email>_<timestamp>.json`
- 账户：`openai-register/tokens/accounts.txt`（email----password）

## 注意事项
- 需可访问 https://auth.openai.com，代理避免 CN/HK 区。 
- 验证码收不到：换代理或重试。
- workspace 为空：多次重试，确保验证码正确。
