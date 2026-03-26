#!/usr/bin/env python3
"""
分析 OpenAI OAuth 流程，寻找绕过 add_phone 的方法
"""
import json

print("="*60)
print("OpenAI OAuth Flow Analysis")
print("="*60)

# 当前使用的 OAuth 参数
current_params = {
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",  # Codex CLI
    "redirect_uri": "http://localhost:1455/auth/callback",
    "scope": "openid email profile offline_access",
    "response_type": "code",
    "prompt": "login",
    "code_challenge_method": "S256",
    "id_token_add_organizations": "true",
    "codex_cli_simplified_flow": "true",
}

print("\n[Current OAuth Parameters]")
for k, v in current_params.items():
    print(f"  {k}: {v}")

# 可尝试的变体
print("\n[Potential Bypass Strategies]")
print("""  
1. **修改 prompt 参数**
   - 'login' (当前): 强制重新登录
   - 'none': 静默认证，可能跳过某些检查
   - 'consent': 强制显示同意页面
   - 尝试: prompt=none 或不传 prompt

2. **修改 scope 参数**
   - 当前: 'openid email profile offline_access'
   - 最小: 'openid email' - 减少权限请求
   - 添加 audience: 'openid email https://api.openai.com/v1'
   
3. **添加额外参数**
   - 'access_type=offline': 请求离线访问
   - 'response_mode=fragment': 使用 fragment 模式
   - 'acr_values': 认证上下文引用
   
4. **修改 client_id**
   - ChatGPT Web: pdlLIXzY84OZ7fCRs5fi5SgPqYJlUJfK
   - Platform: TdJIcbe16WoTHtN95nyywhwEweyuuwVr
   - 不同 client 可能有不同的风控策略
   
5. **跳过 workspace/select**
   - 直接调用 /oauth/token 端点
   - 检查是否有其他获取 token 的方式

6. **使用不同的认证流程**
   - Sign in with Apple/Google
   - API Key 直接创建 (如果已有账户)
""")

# 分析 API 端点
print("\n[API Endpoints Used]")
endpoints = [
    ("/oauth/authorize", "获取授权码入口"),
    ("/oauth/token", "交换 token"),
    ("/api/accounts/authorize/continue", "提交邮箱"),
    ("/api/accounts/password/verify", "验证密码"),
    ("/api/accounts/email-otp/send", "发送验证码"),
    ("/api/accounts/email-otp/validate", "验证邮箱 OTP"),
    ("/api/accounts/user/register", "用户注册"),
    ("/api/accounts/create_account", "创建账户"),
    ("/api/accounts/workspace/select", "选择工作空间 (触发 add_phone)"),
]

for endpoint, desc in endpoints:
    print(f"  {endpoint:45} - {desc}")

# 风控触发点分析
print("\n[Risk Control Trigger Analysis]")
print("""  
add_phone 风控触发因素:
  1. IP 信誉 (机房 IP、数据中心 IP、VPN IP)
  2. 邮箱域名 (临时邮箱被标记)
  3. 行为特征 (TLS 指纹、请求频率)
  4. 设备指纹 (browser fingerprint)
  5. Sentinel Token 验证失败

绕过思路:
  A. 使用住宅代理 (真实家庭 IP)
  B. 使用可信邮箱域名 (自建域名邮箱)
  C. 随机化请求间隔
  D. 使用 curl_cffi 模拟真实浏览器指纹
  E. 伪造 Sentinel Token (较难)
""")

# 代码修改建议
print("\n[Code Modification Suggestions]")
print("""
1. 在 workspace/select 之前添加更多请求:
   - 先访问 /api/accounts/me 获取账户信息
   - 检查是否已有默认 workspace

2. 尝试不同的 workspace_id:
   - 空字符串: 可能创建新 workspace
   - 'default': 默认 workspace
   - 完全跳过: 直接跳到 callback

3. 修改 referer 和 origin:
   - 使用 chat.openai.com 的 referer
   - 可能绕过某些来源检查

4. 分析 invalid_auth_step 响应:
   - 检查响应体中的错误详情
   - 可能包含绕过提示
""")

if __name__ == "__main__":
    pass
