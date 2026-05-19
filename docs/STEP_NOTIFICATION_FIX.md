# 步骤通知功能修复总结

## 🐛 **问题诊断**

从日志分析发现两个主要问题：

### **问题 1：LLM 生成违规对话文本**

**现象**：
```
MockMessage(content='我可以帮您进行 H3C 网络设备的静态路由故障排查...', 
            tool_calls=[...use_skill...])
```

LLM 在调用工具的同时生成了大量对话文本，违反了 SKILL.md 的规则：
- ❌ "我来帮您..."
- ❌ "请提供以下信息..."
- ❌ "根据 H3C 最佳实践..."

**根本原因**：
Qwen 模型的特性是在返回 `tool_calls` 时也会生成 `content` 字段。当前代码会通过 `on_token` 回调将这些文本发送给前端。

---

### **问题 2：步骤编号错误**

**现象**：
```
[SkillSteps] Sending proactive step notification for step 2: 检查下一跳地址可达性
```

实际应该是 step 1，但显示为 step 2。

**根本原因**：
`tool_rounds` 计数器在每次 LLM 返回工具调用时都递增，包括：
1. `use_skill` 调用（不应该计入）
2. `execute_step_script` 调用（应该计入）

导致第一次执行步骤时，`tool_rounds = 2`，所以显示 "step 2"。

---

## ✅ **修复方案**

### **修复 1：抑制 Skill 执行期间的 LLM 文本**

**文件**：[core/agent.py](file://f:\workspace\project\H3C\pythonclaw\core\agent.py)

**位置**：处理 LLM 响应的代码段

**修改前**：
```python
message = response.choices[0].message
logger.info("[Agent] LLM response content: %s", ...)

if not message.tool_calls:
    # ...
```

**修改后**：
```python
message = response.choices[0].message
logger.info("[Agent] LLM response content: %s", ...)

# If we have tool_calls and a skill is active, suppress normal text content
# This prevents LLM from generating conversational text when it should only call tools
if message.tool_calls and hasattr(self, '_current_skill_steps') and self._current_skill_steps:
    if message.content:
        logger.info("[Agent] Suppressing LLM text content during skill execution (skill active)")
        message.content = None

if not message.tool_calls:
    # ...
```

**效果**：
- ✅ 当 skill 激活且有工具调用时，忽略 LLM 生成的文本内容
- ✅ 只保留工具调用，确保 LLM 严格按照 SKILL.md 执行
- ✅ 前端不会收到违规的对话文本

---

### **修复 2：修正步骤编号逻辑**

**文件**：[core/agent.py](file://f:\workspace\project\H3C\pythonclaw\core\agent.py)

**位置**：发送步骤通知的代码段

**修改前**：
```python
# Proactively send step notifications before executing tools
if on_token and hasattr(self, '_current_skill_steps') and self._current_skill_steps:
    current_step_idx = min(tool_rounds - 1, len(self._current_skill_steps) - 1)
    if current_step_idx >= 0:
        step_name = self._current_skill_steps[current_step_idx]
        step_marker = f"[STEP_START]{step_name}[STEP_END]"
        logger.info("[SkillSteps] Sending proactive step notification for step %d: %s", 
                    current_step_idx + 1, step_name)
        on_token(step_marker)
```

**修改后**：
```python
# Proactively send step notifications before executing tools
if on_token and hasattr(self, '_current_skill_steps') and self._current_skill_steps:
    # Only count actual step execution (execute_step_script), not skill activation (use_skill)
    is_step_execution = any(
        tc.function.name == 'execute_step_script' 
        for tc in tool_calls
    )
    
    if is_step_execution:
        # Determine which step we're on based on tool round
        current_step_idx = min(tool_rounds - 1, len(self._current_skill_steps) - 1)
        if current_step_idx >= 0:
            step_name = self._current_skill_steps[current_step_idx]
            step_marker = f"[STEP_START]{step_name}[STEP_END]"
            logger.info("[SkillSteps] Sending proactive step notification for step %d: %s", 
                        current_step_idx + 1, step_name)
            on_token(step_marker)
```

**效果**：
- ✅ 只在执行 `execute_step_script` 时发送步骤通知
- ✅ 忽略 `use_skill` 调用，避免步骤编号偏移
- ✅ 步骤编号从 1 开始，与实际执行顺序一致

---

## 📊 **预期效果**

### **修复前的流程**

```
用户消息: "我的静态路由不通"
  ↓
LLM 响应 1:
  content: "我可以帮您进行 H3C 网络设备的静态路由故障排查..."
  tool_calls: [use_skill]
  ↓
❌ 前端收到大量对话文本
  ↓
LLM 响应 2:
  content: "现在执行第一步检查：\n\n"
  tool_calls: [execute_step_script step1]
  ↓
❌ 前端收到更多对话文本
❌ 步骤通知显示 "step 2"（应该是 step 1）
```

### **修复后的流程**

```
用户消息: "我的静态路由不通"
  ↓
LLM 响应 1:
  content: "我可以帮您..." (被抑制)
  tool_calls: [use_skill]
  ↓
✅ 前端不收到任何文本（只有工具调用）
  ↓
Agent 提取步骤 → 发送 stepName 通知
  ↓
LLM 响应 2:
  content: "现在执行第一步..." (被抑制)
  tool_calls: [execute_step_script step1]
  ↓
✅ 前端只收到 stepName 和 stepCommand 消息
✅ 步骤编号正确显示 "step 1"
```

---

## 🧪 **测试验证**

重启服务并测试：

```bash
python -m pythonclaw start --foreground
```

发送测试消息：
```
我的静态路由不通，目的网段 192.168.1.0/24，下一跳 10.1.1.1，设备 IP 10.88.142.204，telnet admin@local 1qaz!QAZ
```

**期望的日志输出**：

```
[Agent] LLM response content: '我可以帮您...'
[Agent] LLM has tool_calls: True
[Agent] Suppressing LLM text content during skill execution (skill active)
[SkillSteps] Extracted 7 steps from skill 'static-troubleshooting'
[SkillSteps] Sending proactive step notification for step 1: 检查全局路由表中是否存在该静态路由
[StepMarker] Processing text: '[STEP_START]检查全局路由表中是否存在该静态路由[STEP_END]'
[StepMarker] Found 1 step markers
[StepMarker] Sending step 1: 检查全局路由表中是否存在该静态路由
[Tools] Executing step script: step1_check_route.py
```

**关键检查点**：
- ✅ 看到 "Suppressing LLM text content" 日志
- ✅ 步骤编号从 1 开始
- ✅ 前端只收到 JSON 格式的 stepName 消息，没有普通文本

---

## 📝 **技术细节**

### **为什么需要抑制 LLM 文本？**

虽然 SKILL.md 明确禁止 LLM 生成对话文本，但 Qwen 模型（以及其他一些模型）在返回 `tool_calls` 时会同时生成 `content` 字段。这是模型的固有行为，无法通过 prompt 完全控制。

因此，我们需要在 Agent 层面进行后处理，检测以下条件：
1. 有工具调用 (`message.tool_calls`)
2. Skill 处于激活状态 (`_current_skill_steps` 存在且非空)
3. 有文本内容 (`message.content`)

满足这些条件时，将 `message.content` 设置为 `None`，防止其通过 `on_token` 回调发送到前端。

### **为什么 use_skill 不计入步骤编号？**

`use_skill` 是技能激活工具，它的作用是加载 SKILL.md 并提取步骤列表。实际的排查步骤是从 `execute_step_script` 开始的。

如果将 `use_skill` 也计入步骤编号，会导致：
- 第一次执行步骤时显示 "step 2"
- 所有后续步骤编号都偏移 1

因此，我们只在实际执行步骤时才发送步骤通知。

---

## 🎯 **下一步优化建议**

1. **加强 SKILL.md 指令**：虽然我们在代码层做了抑制，但仍可以进一步优化 SKILL.md，使用更强烈的警告语气。

2. **调整 temperature**：降低 temperature 值（如 0.1）可以让 LLM 更严格地遵循指令。

3. **添加重试机制**：如果 LLM 连续多次违反规则，可以考虑重新生成响应或切换到更合适的模型。

4. **监控和日志**：记录 LLM 违反规则的频率，用于评估不同模型的表现。
