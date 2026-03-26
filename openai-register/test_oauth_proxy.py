#!/usr/bin/env python3
"""通过代理测试 OpenAI OAuth 入口点"""
import requests
import urllib.parse
import sys

# 获取一个可用的代理
def get_proxy():
    try:
        import sqlite3
        conn = sqlite3.connect('/home/ubuntu/mregister/web_console/runtime/mregister.db')
        c = conn.cursor()
        c.execute("SELECT proxy_url FROM proxies WHERE proxy_url LIKE 'http%' LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception as e:
        print(f"DB error: {e}")
    return None

def test_chatgpt_login(proxy=None):
    """测试 ChatGPT 官方登录流程"""
    print("\n" + "="*60)
    print("Testing ChatGPT Official Login Flow")
    print("="*60)

    s = requests.Session()
    if proxy:
        s.proxies = {'http': proxy, 'https': proxy}
        print(f"Using proxy: {proxy}")

    print("\n[1] Visiting chat.openai.com...")
    try:
        resp = s.get('https://chat.openai.com/', timeout=30)
        print(f"    Status: {resp.status_code}")
        print(f"    URL after redirects: {resp.url[:80]}")

        if 'auth.openai.com' in resp.url:
            print("    Redirected to auth flow")
            parsed = urllib.parse.urlparse(resp.url)
            params = urllib.parse.parse_qs(parsed.query)
            client_id = params.get('client_id', ['N/A'])[0]
            redirect_uri = params.get('redirect_uri', ['N/A'])[0]
            scope = params.get('scope', ['N/A'])[0]
            print(f"    Client ID: {client_id}")
            print(f"    Redirect URI: {redirect_uri[:60]}...")
            print(f"    Scope: {scope}")

        return True
    except Exception as e:
        print(f"    Error: {e}")
        return False

def test_platform_login(proxy=None):
    """测试 Platform 登录流程"""
    print("\n" + "="*60)
    print("Testing Platform (Developer) Login Flow")
    print("="*60)

    s = requests.Session()
    if proxy:
        s.proxies = {'http': proxy, 'https': proxy}
        print(f"Using proxy: {proxy}")

    print("\n[1] Visiting platform.openai.com...")
    try:
        resp = s.get('https://platform.openai.com/', timeout=30)
        print(f"    Status: {resp.status_code}")
        print(f"    URL: {resp.url[:80]}")

        if 'auth.openai.com' in resp.url:
            parsed = urllib.parse.urlparse(resp.url)
            params = urllib.parse.parse_qs(parsed.query)
            client_id = params.get('client_id', ['N/A'])[0]
            redirect_uri = params.get('redirect_uri', ['N/A'])[0]
            print(f"    Client ID: {client_id}")
            print(f"    Redirect URI: {redirect_uri[:60]}...")

        return True
    except Exception as e:
        print(f"    Error: {e}")
        return False

def analyze_auth_flow(proxy=None):
    """分析完整的 auth.openai.com 流程"""
    print("\n" + "="*60)
    print("Analyzing auth.openai.com OAuth Flow")
    print("="*60)

    s = requests.Session()
    if proxy:
        s.proxies = {'http': proxy, 'https': proxy}
        print(f"Using proxy: {proxy}")

    # 使用 Codex CLI 的参数
    client_id = "pdlLIXzY84OZ7fCRs5fi5SgPqYJlUJfK"
    redirect_uri = "http://localhost:1455/auth/callback"
    scope = "openid email profile offline_access"

    auth_url = f"https://auth.openai.com/authorize?client_id={client_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&response_type=code&scope={urllib.parse.quote(scope)}&prompt=login&audience=https://api.openai.com/v1"

    print(f"\n[1] Requesting OAuth authorize page...")
    print(f"    URL: {auth_url[:80]}...")

    try:
        resp = s.get(auth_url, timeout=30, allow_redirects=True)
        print(f"    Status: {resp.status_code}")
        print(f"    Final URL: {resp.url[:80]}")

        # 检查页面内容
        if 'login' in resp.text.lower():
            print("    -> Login page detected")
        if 'signup' in resp.text.lower() or 'create' in resp.text.lower():
            print("    -> Signup option available")

        # 检查 state 参数
        if 'state=' in resp.url:
            parsed = urllib.parse.urlparse(resp.url)
            params = urllib.parse.parse_qs(parsed.query)
            state = params.get('state', ['N/A'])[0]
            print(f"    State: {state[:30]}...")

        return True
    except Exception as e:
        print(f"    Error: {e}")
        return False

def main():
    proxy = get_proxy()
    if not proxy:
        print("No external proxy found, trying without proxy...")
        print("Warning: Server IP may be blocked by Cloudflare")
    else:
        print(f"Found proxy: {proxy}")

    results = {}
    results['chatgpt'] = test_chatgpt_login(proxy)
    results['platform'] = test_platform_login(proxy)
    results['auth_flow'] = analyze_auth_flow(proxy)

    print("\n" + "="*60)
    print("Results Summary:")
    for name, success in results.items():
        status = 'OK' if success else 'FAILED'
        print(f"  {name}: {status}")

if __name__ == '__main__':
    main()