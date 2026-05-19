# 如何确认当前使用的 LLM Provider

## 方法一：使用检查脚本（推荐）

运行以下命令快速检查：

```bash
python check_provider.py
```

**输出示例（使用 H3C AI）**：
```
============================================================
PythonClaw - LLM Provider Check
============================================================

Provider Name: h3c

✅ Using H3C Internal AI Platform
------------------------------------------------------------
Account:       ts_sn
Model:         DEEPSEEK_V3_PRIVATE
Auth URL:      https://api-ai.h3c.com/session/api/user/login
API Endpoint:  https://api-ai.h3c.com/session/ai/chat/deepseek

Status:        🟢 Company Internal Model

============================================================

✓ Confirmed: Using H3C Internal AI Platform
```

**输出示例（使用其他 Provider）**：
```
Provider Name: deepseek

Using External Provider: deepseek
------------------------------------------------------------
Model:         deepseek-chat
Base URL:      https://api.deepseek.com/v1

Status:        🔵 External Provider

✗ Not using H3C AI (currently using: deepseek)
```

---

## 方法二：查看启动日志

启动服务时，会显示 Provider 信息：

```bash
python -m pythonclaw start --foreground
```

**H3C AI 的日志输出**：
```
============================================================
[PythonClaw] LLM Provider: H3C
[PythonClaw] ✅ Using H3C Internal AI Platform
[PythonClaw] Model: DEEPSEEK_V3_PRIVATE
[PythonClaw] Account: ts_sn
[PythonClaw] API Endpoint: https://api-ai.h3c.com/session/ai/chat/deepseek
============================================================
```

**其他 Provider 的日志输出**：
```
============================================================
[PythonClaw] LLM Provider: DEEPSEEK
[PythonClaw] Model: deepseek-chat
============================================================
```

---

## 方法三：查看配置文件

### Windows
```bash
type %USERPROFILE%\.pythonclaw\pythonclaw.json
```

### Linux/Mac
```bash
cat ~/.pythonclaw/pythonclaw.json
```

**查找以下配置**：

**H3C AI 配置**：
```json
{
  "llm": {
    "provider": "h3c",
    "h3c": {
      "account": "ts_sn",
      "password": "ts_sn123",
      "model": "DEEPSEEK_V3_PRIVATE"
    }
  }
}
```

**其他 Provider 配置**：
```json
{
  "llm": {
    "provider": "deepseek",
    "deepseek": {
      "apiKey": "sk-...",
      "model": "deepseek-chat"
    }
  }
}
```

关键判断依据：
- `"provider": "h3c"` 或 `"provider": "h3cai"` → 使用公司模型
- 其他值 → 使用外部模型

---

## 方法四：通过 Web Dashboard API

访问以下 API 端点：

```bash
curl http://localhost:7788/api/config | jq '.config.llm.provider'
```

或者在浏览器中打开：
```
http://localhost:7788/api/config
```

查找 `config.llm.provider` 字段的值。

---

## 方法五：运行时检查（编程方式）

在 Python 代码中检查：

```python
from pythonclaw import config

provider_name = config.get_str("llm", "provider", env="LLM_PROVIDER", default="deepseek")

if provider_name.lower() in ("h3c", "h3cai"):
    print("✅ 正在使用公司内部的 H3C AI 模型")
else:
    print(f"❌ 使用的是外部模型: {provider_name}")
```

---

## 快速判断标准

| 指标 | H3C AI | 其他 Provider |
|------|--------|--------------|
| provider 值 | `h3c` 或 `h3cai` | `deepseek`, `qwen`, `kimi` 等 |
| 认证方式 | account/password | apiKey |
| 模型名称 | `DEEPSEEK_V3_PRIVATE` | `deepseek-chat`, `qwen-plus` 等 |
| API 域名 | `api-ai.h3c.com` | `api.deepseek.com`, `dashscope.aliyuncs.com` 等 |
| 内网访问 | ✅ 支持 | ❌ 通常需要外网 |

---

## 切换到 H3C AI

如果当前不是使用 H3C AI，可以这样切换：

### 方法一：使用 onboard 向导
```bash
python -m pythonclaw onboard
```
选择 "H3C AI (Internal)"，然后输入账号密码。

### 方法二：手动修改配置
编辑 `pythonclaw.json`，将 `llm.provider` 改为 `"h3c"`，并添加 H3C 配置。

### 方法三：使用环境变量
```bash
export LLM_PROVIDER=h3c
python -m pythonclaw start --foreground
```

---

## 常见问题

### Q: 如何确认请求真的发到了公司服务器？

**A**: 查看日志中的网络请求：
```bash
# 启动时添加更详细的日志
PYTHONCLAW_LOG_LEVEL=DEBUG python -m pythonclaw start --foreground
```

应该能看到类似这样的日志：
```
[H3CAI] Token obtained successfully
HTTP Request: POST https://api-ai.h3c.com/session/ai/chat/deepseek
```

### Q: 如果 token 获取失败怎么办？

**A**: 检查以下几点：
1. 账号密码是否正确
2. 网络连接是否正常
3. 防火墙是否允许访问 `api-ai.h3c.com`
4. 查看错误日志：`context/logs/` 目录

### Q: 如何测试 H3C AI 是否正常工作？

**A**: 运行测试脚本：
```python
from pythonclaw.core.llm.h3c_ai_provider import H3CAIProvider
import asyncio

async def test():
    provider = H3CAIProvider()
    response = await provider._chat_async(
        messages=[{"role": "user", "content": "你好"}],
        stream=False
    )
    print(response.choices[0].message.content)

asyncio.run(test())
```
