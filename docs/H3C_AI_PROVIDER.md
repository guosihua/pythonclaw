# H3C AI Provider 使用说明

## 概述

H3C AI Provider 允许您使用公司内部的 DeepSeek API 平台作为大模型提供者。

## 配置方式

### 方法一：使用 onboard 向导（推荐）

运行 onboard 命令并选择 "H3C AI (Internal)"：

```bash
python -m pythonclaw onboard
```

按照提示输入：
1. 选择 provider: 输入对应 H3C AI 的编号
2. Account: 输入您的账号（默认: ts_sn）
3. Password: 输入您的密码（默认: ts_sn123）

### 方法二：手动编辑配置文件

编辑 `pythonclaw.json` 文件：

```json
{
  "llm": {
    "provider": "h3c",
    "h3c": {
      "account": "ts_sn",
      "password": "ts_sn123",
      "model": "DEEPSEEK_V3_PRIVATE",
      "authUrl": "https://api-ai.h3c.com/session/api/user/login",
      "apiEndpoint": "https://api-ai.h3c.com/session/ai/chat/deepseek"
    }
  }
}
```

### 方法三：使用环境变量

```bash
export LLM_PROVIDER=h3c
export H3C_ACCOUNT=ts_sn
export H3C_PASSWORD=ts_sn123
export H3C_MODEL=DEEPSEEK_V3_PRIVATE
```

## 配置参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `account` | H3C AI 平台账号 | `ts_sn` |
| `password` | H3C AI 平台密码 | `ts_sn123` |
| `model` | 使用的模型名称 | `DEEPSEEK_V3_PRIVATE` |
| `authUrl` | 认证接口地址 | `https://api-ai.h3c.com/session/api/user/login` |
| `apiEndpoint` | 聊天接口地址 | `https://api-ai.h3c.com/session/ai/chat/deepseek` |

## 工作流程

1. **认证**：Provider 首先调用认证接口获取 token
2. **缓存**：Token 会被缓存，避免重复认证
3. **聊天**：使用 token 调用聊天接口
4. **自动刷新**：如果 token 过期，会自动重新认证

## 示例代码

```
from pythonclaw.core.llm.h3c_ai_provider import H3CAIProvider

# 创建 Provider
provider = H3CAIProvider(
    account="your_account",
    password="your_password"
)

# 同步调用
response = provider.chat(
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)

# 流式调用（目前回退到非流式）
for chunk in provider.chat_stream(
    messages=[{"role": "user", "content": "你好"}]
):
    print(chunk["text"])
```

## 注意事项

1. **SSL 验证**：H3C AI Provider 默认跳过 SSL 验证（`TCPConnector(ssl=False)`）
2. **异步支持**：Provider 内部使用 aiohttp 进行异步调用
3. **Token 管理**：Token 会自动缓存和刷新，无需手动管理
4. **流式输出**：✅ H3C API **支持流式输出**，设置 `"stream": True` 即可启用

## 故障排查

### 认证失败

检查账号密码是否正确：
```bash
curl -X POST https://api-ai.h3c.com/session/api/user/login \
  -H "Auth-Type: DB" \
  -H "Content-Type: application/json" \
  -d '{"account": "ts_sn", "password": "ts_sn123"}'
```

### 连接超时

检查网络连接和防火墙设置，确保可以访问 `api-ai.h3c.com`。

### Token 过期

Provider 会自动处理 token 过期和刷新，如果仍然有问题，请重启服务。

## 与其他 Provider 的对比

| 特性 | H3C AI | DeepSeek | Qwen |
|------|--------|----------|------|
| 认证方式 | Account/Password | API Key | API Key |
| 流式输出 | ✅ | ✅ | ✅ |
| 工具调用 | ✅ | ✅ | ✅ |
| 多模态 | ❌ | ✅ | ✅ |
| 内网访问 | ✅ | ❌ | ❌ |
