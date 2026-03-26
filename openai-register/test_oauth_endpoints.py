#!/usr/bin/env python3
"""测试 OpenAI 不同的 OAuth 入口点"""
import requests
import urllib.parse

# 不同的 client_id 和入口
ENDPOINTS = {
    # ChatGPT Web
    'chatgpt_web': {
        'client_id': 'pdlLIXzY84OZ7fCRs5fi5SgPqYJlUJfK',
        'redirect_uri': 'https://chat.openai.com/api/auth/callback',
        'scope': 'openid email profile offline_access',
    },
    # Platform (开发者平台)
    'platform': {
        'client_id': 'TdJIcbe16WoTHtN95nyywhwEweyuuwVr',
        'redirect_uri': 'https://platform.openai.com/auth/callback',
        'scope': 'openid email profile offline_access',
    },
    # API
    'api': {
        'client_id': 'pdlLIXzY84OZ7fCRs5fi5SgPqYJlUJfK',
        'redirect_uri': 'https://api.openai.com/v1/auth/callback',
        'scope': 'openid email profile offline_access',
    },
    # Codex CLI (当前使用的)
    'codex_cli': {
        'client_id': 'pdlLIXzY84OZ7fCRs5fi5SgPqYJlUJfK',
        'redirect_uri': 'http://localhost:1455/auth/callback',
        'scope': 'openid email profile offline_access',
    },
    # Apps (应用授权)
    'apps': {
        'client_id': 'pdlLIXzY84OZ7fCRs5fi5SgPqYJlUJfK',
        'redirect_uri': 'https://auth.openai.com/authorize',
        'scope': 'openid email profile offline_access',
    },
}

def test_endpoint(name, config, proxy=None):
    """测试单个端点"""
    print(f"\n{'='*50}")
    print(f"Testing: {name}")
    print(f"Client ID: {config['client_id'][:20]}...")
    print(f"Redirect URI: {config['redirect_uri']}")
    
    auth_url = "https://auth.openai.com/oauth/authorize"
    params = {
        'client_id': config['client_id'],
        'response_type': 'code',
        'redirect_uri': config['redirect_uri'],
        'scope': config['scope'],
        'prompt': 'login',
    }
    
    url = f"{auth_url}?{urllib.parse.urlencode(params)}"
    
    try:
        s = requests.Session()
        if proxy:
            s.proxies = {'http': proxy, 'https': proxy}
        
        resp = s.get(url, allow_redirects=False, timeout=15)
        print(f"Status: {resp.status_code}")
        print(f"Location: {resp.headers.get('Location', 'N/A')[:100]}")
        
        if resp.status_code == 302:
            location = resp.headers.get('Location', '')
            if 'error' in location:
                print(f"Error in redirect: {location}")
            else:
                print(f"Valid redirect to: {location[:80]}...")
        elif resp.status_code == 200:
            # 检查页面内容
            if 'error' in resp.text.lower():
                print("Page contains error")
            else:
                print("Got login page (200 OK)")
        
        # 保存 cookies 信息
        cookies = [c.name for c in s.cookies]
        print(f"Cookies set: {cookies}")
        
    except Exception as e:
        print(f"Error: {e}")

def main():
    print("OpenAI OAuth Endpoints Test")
    print("="*50)
    
    # 测试所有端点
    for name, config in ENDPOINTS.items():
        test_endpoint(name, config)
    
    print("\n" + "="*50)
    print("Test complete")

if __name__ == '__main__':
    main()
