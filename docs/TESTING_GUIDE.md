# 静态路由排查功能测试指南

## 🎯 测试目标

验证使用 Qwen 模型时，系统是否能正确执行静态路由排查流程。

---

## 📋 测试步骤

### **1. 启动服务**

```bash
python -m pythonclaw start --foreground
```

确认日志显示：
```
[PythonClaw] LLM Provider: QWEN
[PythonClaw] Model: qwen3.5-flash
```

### **2. 打开 Web Dashboard**

访问：`http://localhost:7788`

### **3. 发送测试消息**

#### **测试用例 1：完整信息**
```
我的静态路由不通，目的网段 192.168.1.0/24，下一跳 10.1.1.1，设备 IP 10.88.142.204，telnet admin@local 1qaz!QAZ
```

#### **测试用例 2：简化信息**
```
我的静态路由不通
```

#### **测试用例 3：部分信息**
```
检查静态路由，目的网段 10.10.0.0/16
```

---

## 🔍 观察要点

### **1. LLM 的初始响应**

**✅ 期望行为**：
- LLM 直接调用 `use_skill` 工具
- 不生成任何对话文本（如"我来帮您..."）

**❌ 不良行为**：
- LLM 输出大量分析文本
- LLM 询问用户更多信息

### **2. 步骤通知**

**前端应该收到以下消息类型**：

| 消息类型 | 触发时机 | 示例内容 |
|---------|---------|---------|
| `stepName` | Agent 提取步骤后 | `"检查全局路由表中是否存在该静态路由"` |
| `stepCommand` | LLM 调用 execute_step_script | `"dis ip routing-table 192.168.1.0 24"` |

**观察方法**：
- 打开浏览器开发者工具（F12）
- 切换到 Network 标签
- 查看 `/chatbot/dm/claw/stream` 请求的响应
- 或者在前端代码中添加 console.log

### **3. 工具执行流程**

**正常流程**：
```
用户消息
  ↓
LLM 调用 use_skill
  ↓
Agent 提取步骤 → 发送 stepName
  ↓
LLM 调用 execute_step_script (step1)
  ↓
Agent 发送 stepCommand
  ↓
脚本执行 → 返回结果给 LLM
  ↓
LLM 根据 next_step 决定下一步
  ↓
重复直到完成
```

### **4. 日志检查**

在终端中观察日志：

**应该看到**：
```
[SkillSteps] Extracted X steps from skill 'static-troubleshooting'
[SkillSteps] Sending proactive step notification for step 1: 检查全局路由表中是否存在该静态路由
[Tools] Executing step script: step1_check_route.py
[Tools] Step script result: {...}
```

**不应该看到**：
- LLM 生成的对话文本（除了最终总结）
- 错误信息或异常堆栈

---

## 🐛 常见问题诊断

### **问题 1：LLM 不调用工具，而是生成对话**

**症状**：
- 前端收到大量普通文本消息
- 没有收到 `stepName` 或 `stepCommand`

**可能原因**：
- SKILL.md 指令不够强
- Temperature 设置过高
- Qwen 模型对指令的理解有偏差

**解决方案**：
1. 降低 temperature（在配置文件中设置）
2. 加强 SKILL.md 中的禁止规则
3. 在 system prompt 中添加额外约束

### **问题 2：步骤通知未发送**

**症状**：
- 工具正常执行
- 但前端没有收到 `stepName` 消息

**可能原因**：
- [extract_steps_from_messages](file://f:\workspace\project\H3C\pythonclaw\core\agent.py#L1068-L1105) 函数未正确提取步骤
- SKILL.md 格式不正确

**检查方法**：
查看日志中是否有：
```
[SkillSteps] Extracted X steps from skill 'static-troubleshooting'
```

如果没有，说明步骤提取失败。

### **问题 3：工具执行失败**

**症状**：
- LLM 调用了 `execute_step_script`
- 但返回错误信息

**可能原因**：
- 脚本文件不存在
- 参数传递错误
- 设备连接失败

**检查方法**：
查看日志中的 `[Tools]` 相关输出

---

## 📊 测试结果记录

### **测试用例 1：完整信息**

- [ ] LLM 是否直接调用工具？
- [ ] 是否收到 stepName 消息？
- [ ] 是否收到 stepCommand 消息？
- [ ] 工具是否正常执行？
- [ ] 最终是否得到正确的诊断结果？

### **测试用例 2：简化信息**

- [ ] LLM 是否使用了默认值？
- [ ] 流程是否正常进行？
- [ ] 是否有额外的询问？

### **测试用例 3：部分信息**

- [ ] LLM 如何处理缺失的参数？
- [ ] 是否在第一步后获取了缺失的信息？

---

## 💡 优化建议

根据测试结果，可能需要：

### **1. 如果 LLM 仍然生成对话**

修改 SKILL.md，在开头添加更强烈的警告：

```markdown
# ⚠️ 紧急警告

如果你输出任何对话文本（而不是调用工具），系统将立即崩溃！

你必须：
1. 只调用 execute_step_script 工具
2. 不要说任何话
3. 不要解释你的行为
4. 不要询问用户
```

### **2. 如果步骤通知未工作**

检查 [extract_steps_from_messages](file://f:\workspace\project\H3C\pythonclaw\core\agent.py#L1068-L1105) 函数的正则表达式是否正确匹配 `[STEP_START]...[STEP_END]` 格式。

### **3. 如果工具执行有问题**

检查脚本文件的权限和路径是否正确。

---

## 📝 反馈收集

测试完成后，请记录：

1. **哪个测试用例成功了？**
2. **哪个测试用例失败了？**
3. **具体的错误现象是什么？**
4. **日志中有什么异常信息？**

根据这些信息，我们可以进一步优化系统！
