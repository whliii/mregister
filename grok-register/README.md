# Grok (x.ai) 注册机使用教程

## 环境准备

1. 创建并激活虚拟环境（Python 3.10+）：

```bash
cd grok-register
python3 -m venv venv
source venv/bin/activate    # Windows 用 venv\Scripts\activate
```

2. 安装依赖：

```bash
pip install curl_cffi beautifulsoup4 requests python-dotenv
```

3. 准备输出目录：

```bash
mkdir -p keys
```

4. 配置 YesCaptcha（必需）：在 `.env` 写入

```
YESCAPTCHA_KEY="你的_yescaptcha_key"
```

5. 如需代理，编辑 `grok.py` 顶部的 `PROXIES`。

## 运行

```bash
cd grok-register
source venv/bin/activate
python grok.py
# 提示输入并发数，回车默认 8
```

成功后输出：

- `keys/grok.txt`：SSO token 列表
- `keys/accounts.txt`：email:password:SSO

## 注意事项

- 必须有 YesCaptcha 余额并配置 YESCAPTCHA_KEY。
- 若初始化提示“未找到 Action ID”，请更换代理或重试。
