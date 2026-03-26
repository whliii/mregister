#!/usr/bin/env python3
"""
测试不同的 OAuth 参数组合，尝试绕过 add_phone 风控
"""
import sys
sys.path.insert(0, '/home/ubuntu/mregister/openai-register')

import json
import time
import urllib.parse
from curl_cffi import requests

# 不同的 OAuth 配置测试
TEST_CONFIGS = [
    {
        "name": "original (current)",
        "prompt": "login",
        "scope": "openid email profile offline_access",
        "extra_params": {"codex_cli_simplified_flow": "true"},
    },
    {
        "name": "no_prompt",
        "prompt": None,  # 不发送 prompt 参数
        "scope": "openid email profile offline_access",
        "extra_params": {},
    },
    {
        "name": "prompt_none",
        "prompt": "none",
        "scope": "openid email profile offline_access",
        "extra_params": {},
    },
    {
        "name": "minimal_scope",
        "prompt": "login",
        "scope": "openid email",
        "extra_params": {},
    },
    {
        "name": "with_audience",
        "prompt": "login",
        "scope": "openid email profile offline_access",
        "extra_params": {"audience": "https://api.openai.com/v1"},
    },
    {
        "name": "no_codex_flow",
        "prompt": "login",
        "scope": "openid email profile offline_access",
        "extra_params": {},  # 不使用 codex_cli_simplified_flow
    },
]

def test_oauth_config(config, proxy=None):
    """测试单个 OAuth 配置"""
    s = requests.Session(impersonate="chrome120")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    
    client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
    redirect_uri = "http://localhost:1455/auth/callback"
    
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
    }
    
    if config["prompt"]:
        params["prompt"] = config["prompt"]
    if config["scope"]:
        params["scope"] = config["scope"]
    params.update(config["extra_params"])
    
    url = f"https://auth.openai.com/authorize?{urllib.parse.urlencode(params)}"
    
    try:
        resp = s.get(url, timeout=15, allow_redirects=True)
        result = {
            "status": resp.status_code,
            "url": resp.url[:80] if len(resp.url) > 80 else resp.url,
            "has_login": "login" in resp.text.lower(),
            "has_signup": "sign up" in resp.text.lower() or "signup" in resp.text.lower(),
        }
        return result
    except Exception as e:
        return {"error": str(e)}

def main():
    proxy = "http://127.0.0.1:7890"
    print(f"Testing OAuth bypass strategies")
    print(f"Proxy: {proxy}")
    print("="*60)
    
    for config in TEST_CONFIGS:
        print(f"\n[{config['name']}]")
        result = test_oauth_config(config, proxy)
        
        if "error" in result:
            print(f"  Error: {result['error']}")
        else:
            print(f"  Status: {result['status']}")
            print(f"  URL: {result['url']}")
            print(f"  Has login: {result['has_login']}")
            print(f"  Has signup: {result['has_signup']}")
        
        time.sleep(1)  # 避免请求过快
    
    print("\n" + "="*60)
    print("Test complete")

if __name__ == "__main__":
    main()
