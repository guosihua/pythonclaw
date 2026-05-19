# SKILL.md 重构说明

## 🎯 **重构目标**

解决以下问题：
1. ❌ LLM 没有正确调用工具
2. ❌ 缺少明确的工具调用格式说明
3. ❌ 没有说明如何获取回显结果
4. ❌ 步骤描述不够强制，LLM 容易生成对话文本

---

## ✅ **主要改进**

### **1. 强化角色定义**

**修改前**：
```markdown
请扮演一名资深网络运维专家...
```

**修改后**：
```markdown
# ⚠️ 自动化脚本执行器 - 静态路由故障排查

你现在的角色是一个自动化脚本执行器，不是对话助手。
```

**效果**：明确告诉 LLM 它不是对话助手，而是自动化工具执行器。

---

### **2. 添加绝对规则（违反将导致系统崩溃）**

新增章节：
```markdown
## 🚨 绝对规则（违反将导致系统崩溃）

1. ❌ 禁止输出任何对话文本
2. ❌ 禁止询问用户信息
3. ✅ 只调用 execute_step_script 工具
4. ✅ 严格按顺序执行
5. ✅ 根据 next_step 字段跳转
```

**效果**：使用强烈的警告语气，让 LLM 明白违反规则的严重后果。

---

### **3. 明确工具调用规范**

新增章节：
```markdown
## 🛠️ 工具调用规范

### 工具名称：execute_step_script

### 调用格式：
{
  "script_name": "stepX_script_name.py",
  "mode": "build",
  "params": {...}
}

### 工具返回格式：
{
  "answerType": "stepCommand",
  "currentStep": 1,
  "message": {...},
  "analysis_result": {
    "route_exists": true,
    "next_step": "step2"
  }
}
```

**效果**：
- ✅ 明确工具名称
- ✅ 提供完整的 JSON 格式示例
- ✅ 说明返回结果的结构
- ✅ 强调 `next_step` 字段的重要性

---

### **4. 详细说明每个步骤的执行流程**

**修改前**：
```markdown
**执行动作**：
1. 调用python脚本，将执行命令作为参数传给python脚本...
```

**修改后**：
```markdown
**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
{
  "script_name": "step1_check_route.py",
  "mode": "build",
  "params": {...}
}

**等待工具返回**，然后从返回结果中提取 `analysis_result.next_step`：
- 如果 `next_step == "step2"` → 执行【第二步】
- 如果 `next_step == "step5"` → 跳转到【第五步】
```

**效果**：
- ✅ 明确每一步都要调用工具
- ✅ 提供具体的参数格式
- ✅ 说明如何根据返回结果决定下一步
- ✅ 清晰的条件分支逻辑

---

### **5. 添加关键参数提取表格**

新增章节：
```markdown
## 🔧 关键参数提取

| 参数 | 提取方式 | 默认值 |
|------|---------|--------|
| destination_network | 从用户问题中提取目的网段 | "0.0.0.0/0" |
| nexthop_ip | 从用户问题中提取下一跳IP | 第一步后从路由表中提取 |
| device_info.ip | 从上下文提取设备IP | "" |
| ... | ... | ... |

**重要**：不要询问用户这些参数，直接使用或设置默认值。
```

**效果**：
- ✅ 清晰的参数列表
- ✅ 明确的默认值
- ✅ 强调不要询问用户

---

### **6. 添加执行示例**

新增章节：
```markdown
## ⚡ 执行示例

### 场景 1：用户提供完整信息
**用户输入**：我的静态路由不通...
**你应该做的**：
1. 调用 use_skill 激活技能
2. 立即调用 execute_step_script 执行 step1
3. 不要输出任何文本

**你不应该做的**：
- ❌ "我来帮您排查..."
- ❌ "请提供以下信息..."
```

**效果**：通过具体示例告诉 LLM 什么是对的、什么是错的。

---

### **7. 添加工具返回结果说明**

在每个步骤中明确说明：
```markdown
**等待工具返回**，然后从返回结果中提取 `analysis_result.next_step`：
- 如果 `next_step == "step2"` → 执行【第二步】
- 如果 `next_step == "step5"` → 跳转到【第五步】
```

**效果**：
- ✅ LLM 知道工具会返回分析结果
- ✅ LLM 知道如何解析返回结果
- ✅ LLM 知道如何根据结果决定下一步

---

### **8. 强化最后一步的总结权限**

**修改前**：无明确说明

**修改后**：
```markdown
### 第八步：流程结束与总结

**此时你可以输出少量总结文本**，例如：
```
静态路由故障排查完成。
诊断结果：<根据前面步骤的分析结果总结>
建议：<给出具体建议>
```
```

**效果**：明确告诉 LLM 只有在最后一步才能输出总结文本。

---

## 📊 **对比总结**

| 方面 | 修改前 | 修改后 |
|------|--------|--------|
| **角色定义** | "请扮演网络运维专家" | "自动化脚本执行器" |
| **规则强度** | 软性建议 | 强制性规则（违反将崩溃） |
| **工具说明** | 模糊提及 | 详细的 JSON 格式示例 |
| **返回结果** | 未说明 | 明确的结构和字段说明 |
| **参数处理** | 未明确 | 表格化列出默认值 |
| **步骤流程** | 描述性语言 | 明确的调用指令和条件分支 |
| **示例** | 无 | 正反两个场景示例 |
| **总结权限** | 未限制 | 仅最后一步可输出 |

---

## 🎯 **预期效果**

重启服务后，LLM 的行为应该是：

### **第一次调用（use_skill）**
```json
{
  "tool_calls": [
    {
      "function": {
        "name": "use_skill",
        "arguments": "{\"skill_name\": \"static-troubleshooting\"}"
      }
    }
  ],
  "content": null  // 被 Agent 抑制
}
```

### **第二次调用（execute_step_script step1）**
```json
{
  "tool_calls": [
    {
      "function": {
        "name": "execute_step_script",
        "arguments": "{\"script_name\": \"step1_check_route.py\", \"mode\": \"build\", \"params\": {...}}"
      }
    }
  ],
  "content": null  // 被 Agent 抑制
}
```

### **第三次调用（根据 next_step 决定）**
```json
{
  "tool_calls": [
    {
      "function": {
        "name": "execute_step_script",
        "arguments": "{\"script_name\": \"step2_check_nexthop.py\", ...}"
      }
    }
  ],
  "content": null
}
```

### **最后一步（总结）**
```json
{
  "tool_calls": null,
  "content": "静态路由故障排查完成。\n\n诊断结果：...\n\n建议：..."
}
```

---

## 🧪 **测试验证**

重启服务并测试：

```bash
python -m pythonclaw start --foreground
```

发送测试消息：
```
我的静态路由不通
```

**期望的日志输出**：
```
[Agent] LLM response content: '' (或被抑制)
[Agent] LLM has tool_calls: True
[SkillSteps] Extracted 8 steps from skill 'static-troubleshooting'
[SkillSteps] Sending proactive step notification for step 1: 检查全局路由表中是否存在该静态路由
[Tools] Executing step script: step1_check_route.py
[Tools] Script output: {"answerType": "stepCommand", ...}
```

**关键检查点**：
- ✅ LLM 不调用工具时会被强制纠正
- ✅ LLM 不生成对话文本
- ✅ 步骤通知正常发送
- ✅ 工具正常执行

---

## 💡 **进一步优化建议**

如果 LLM 仍然不听话，可以考虑：

1. **降低 temperature**：在配置文件中设置 `temperature: 0.1`
2. **添加强制系统提示**：在每次调用时动态添加系统提示
3. **切换到更强的模型**：如 GPT-4、Claude 等指令遵循度更高的模型
4. **添加重试机制**：如果 LLM 连续多次违规，重新生成响应
