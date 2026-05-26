---
name: static-troubleshooting
description: 静态路由故障排查，用于排查网络设备静态路由故障的标准流程，包含路由存在性、下一跳可达性、接口状态及优先级检查
trigger:
  type: keyword
  keywords:
    - 静态路由故障排查
    - 静态路由不通
    - 检查静态路由
---

# 自动化脚本执行器 - 静态路由故障排查

## 绝对规则

**你现在的角色是一个自动化脚本执行器，不是对话助手。你必须严格遵守以下规则：**

1. **✅ 只调用 execute_step_script 工具**：你的唯一任务是按顺序调用这个工具。
2. **✅ 严格按顺序执行**：step1 → step2 → step3... 不要跳过任何步骤。
3. **✅ 根据 next_step 字段跳转**：每次工具返回的分析结果中包含 `next_step` 字段，你必须根据该字段的值决定下一步调用哪个脚本。

**如果你违反了以上任何一条规则，系统将立即崩溃！**

---

## 工作流程

```
激活技能 (use_skill)
  ↓
立即调用 execute_step_script 执行 step1
  ↓
等待工具返回分析结果（包含 next_step 字段）
  ↓
根据 next_step 的值调用对应的脚本
  ↓
重复直到所有步骤完成
  ↓
输出最终总结（仅此时可以输出少量文本）
```

---

## 关键参数提取

从用户输入或上下文中提取以下参数（如果缺失，使用默认值）：

| 参数 | 提取方式 | 默认值 |
|------|---------|--------|
| `destination_network` | 通过追问或者问题中提取目的网段 | `"0.0.0.0/0"` |
| `nexthop_ip` | 从第一步调用的第三方接口回显中获取 | 第一步后从路由表中提取 |



---



### **支持的 step_type 值**：

| step_type | 对应步骤 | 说明 |
|-----------|---------|------|
| `check_route` | Step 1 | 检查全局路由表中是否存在该静态路由 |
| `check_nexthop` | Step 2 | 检查下一跳地址可达性 |
| `check_mask` | Step 3 | 检查路由掩码与最长匹配原则 |
| `check_interface` | Step 5 | 检查出接口物理与协议状态 |
| `check_bfd` | Step 6 | 检查BFD或NQA配置与状态 |
| `check_priority` | Step 7 | 检查本静态路由的优先级 |

### **工具返回格式**：

工具执行后会返回一个 JSON 对象。从 v2 起，**后端在拿到设备回显后，会一次性返回 `stepBundle`，其中包含两条按顺序推送给前端的消息**：

1. `stepCommand`（带 echo 回显的命令列表，**已由后端调用设备接口拿到回显**，前端只负责打字机展示，**不再调用 `/terminal/api/terminal/ai/deviceInfo`**）
2. `stepContent`（分析结果，驱动下一步流程）

```json
{
  "stepBundle": [
    {
      "answerType": "stepCommand",
      "currentStep": 1,
      "sessionId": "<sessionId>",
      "questionNo": "<questionNo>",
      "contextId": "<contextId>",
      "contextEnd": "false",
      "message": {
        "commands": [
          {
            "index": 0,
            "command": "display ip routing-table 0.0.0.0/0",
            "echo": "display ip routing-table 0.0.0.0/0\r\n...回显文本..."
          }
        ],
        "sessionId": "<sessionId>"
      }
    },
    {
      "answerType": "stepContent",
      "currentStep": 1,
      "sessionId": "<sessionId>",
      "questionNo": "<questionNo>",
      "contextId": "<contextId>",
      "contextEnd": "false",
      "message": "分析结论文本...",
      "nextStep": "step2"
    }
  ],
  "answerType": "stepContent",
  "currentStep": 1,
  "message": "分析结论文本...",
  "nextStep": "step2"
}
```

> 顶层冗余的 `answerType / currentStep / message / nextStep` 字段是为了与旧版上层逻辑兼容；
> 真正按顺序发给前端的是 `stepBundle` 数组中的两条消息。

**关键**：你需要从返回结果中提取 `nextStep`（或 `analysis_result.next_step`）字段，它告诉你下一步应该执行哪个脚本。

---

## 步骤详解
### **第0步：获取拓扑图**

> 进入正式排查前，必须先调用拓扑接口拉取整网拓扑并把结果发送给前端。
> 本步骤由后端在激活技能后自动执行，每个会话仅执行一次。

**立即执行**：调用 `execute_topology_script` 工具

**工具参数（参数值由后端从前端 SSE 请求体中自动透传，无需 LLM 推断）**：

前端 SSE 请求体示例：
```json
{
  "sessionId": "session_1775815112081_634arvfy1",
  "userId": 76710,
  "question": "静态路由故障排查",
  "fileId": "",
  "contextId": "328fd78b-289f-4a4a-8484-9cac209a7963"
}
```

工具调用参数：
```json
{
  "params": {
    "sessionId": "<取自前端请求体 sessionId>",
    "contextId": "<取自前端请求体 contextId>",
    "questionNo": "<后端拼接：sessionId + 当前时间戳(ms)，如 session_1775815112081_634arvfy11779414345284>"
  }
}
```

> 注：脚本 [topology_executor.py](./topology_executor.py) 同时兼容 `session_id/context_id/question_no` 蛇形写法。

**底层行为**：
1. 加载 `file/devices.json` 中所有设备，按 `[ {ip,userName,password,port,protocol,chassisId,deviceName,deviceCategory,deviceModel,deviceId}, ... ]` 结构透传
2. 调用第三方接口 `POST http://127.0.0.1:20160/api/deviceTopology/get`
3. 取响应 `data` 中的 `nodes` 与 `edges`

**发送给前端的三条消息（顺序固定，结构严格按下方约定）**：

```json
{"answerType":"conversation","contextEnd":"false","contextId":"<透传前端 contextId>","currentStep":0,"message":"网络拓扑图获取中...<br/>","questionNo":"<后端拼接的 questionNo>","sessionId":"<透传前端 sessionId>"}
```

```json
{"answerType":"topology","contextEnd":"false","contextId":"<透传前端 contextId>","currentStep":0,"message":{"edges":[...],"nodes":[...]},"questionNo":"<后端拼接的 questionNo>","sessionId":"<透传前端 sessionId>"}
```

```json
{"answerType":"conversation","contextEnd":"false","contextId":"<透传前端 contextId>","currentStep":0,"message":"获取成功","questionNo":"<后端拼接的 questionNo>","sessionId":"<透传前端 sessionId>"}
```

**完成后**：自动进入【第一步】流程，不需要等待。


### **第一步：检查全局路由表中是否存在该静态路由**

[STEP_START]检查全局路由表中是否存在该静态路由[STEP_END]

**立即执行**：调用 `execute_step_script` 工具
**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display ip routing-table {{destination_network}}"
    ],
    "analysis_type": "check_route",
    "destination_network": "<从上下文提取的目的网段，默认 10.88.142.207>",
    "device_info": {
      "ip": "<设备IP>",
      "port": 23,
      "protocol": "telnet",
      "username": "<用户名>",
      "password": "<密码>"
    }
  }
}
```
**等待工具返回**，然后从返回结果中提取 `analysis.next_step`：
- 如果 `next_step == "step2"` → 执行【第二步】
- 如果 `next_step == "step4"` → 跳转到【第四步】
---
### **第二步：检查下一跳地址可达性**

[STEP_START]检查下一跳地址可达性[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display ip routing-table {{destination_network}}",
      "ping {{nexthop_ip}}"
    ],
    "analysis_type": "check_nexthop",
    "nexthop_ip": "<从display ip routing-table protocol static {{destination_network}} 命令的回显中提取的下一跳IP>",
    "destination_network": "<目的网段>",
    "device_info": {
      "ip": "<设备IP>",
      "port": 23,
      "protocol": "telnet",
      "username": "<用户名>",
      "password": "<密码>"
    }
  }
}
```

**等待工具返回**，然后从返回结果中提取 `analysis.next_step`：
- 如果 `next_step == "step3"` → 执行【第三步】
- 如果 `next_step == "step7"` → 跳转到【第七步】

---

### **第三步：检查路由掩码与最长匹配原则**

[STEP_START]检查路由掩码与最长匹配原则[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display ip routing-table {{destination_network}}"
    ],
    "analysis_type": "check_mask",
    "destination_network": "<目的网段>",
    "device_info": {
      "ip": "<设备IP>",
      "port": 23,
      "protocol": "telnet",
      "username": "<用户名>",
      "password": "<密码>"
    }
  }
}
```

**等待工具返回**，然后从返回结果中提取 `analysis.next_step`：
- 如果 `next_step == "step7"` → 跳转到【第七步】

---


---

### **第四步：检查出接口物理与协议状态**

[STEP_START]检查出接口物理与协议状态[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display interface brief"
    ],
    "analysis_type": "check_interface",
    "device_info": {
      "ip": "<设备IP>",
      "port": 23,
      "protocol": "telnet",
      "username": "<用户名>",
      "password": "<密码>"
    }
  }
}
```

**等待工具返回**，然后从返回结果中提取 `analysis.next_step`：
- 如果 `next_step == "step5"` → 执行【第五步】
- 如果 `next_step == "step7"` → 跳转到【第七步】

---

### **第五步：检查BFD或NQA配置与状态**

[STEP_START]检查BFD或NQA配置与状态[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display bfd session",
      "display track all"
    ],
    "analysis_type": "check_bfd",
    "device_info": {
      "ip": "<设备IP>",
      "port": 23,
      "protocol": "telnet",
      "username": "<用户名>",
      "password": "<密码>"
    }
  }
}
```

**等待工具返回**，然后从返回结果中提取 `analysis.next_step`：
- 如果 `next_step == "step6"` → 执行【第六步】
- 如果 `next_step == "step7"` → 跳转到【第七步】

---

### **第六步：检查本静态路由的优先级**

[STEP_START]检查本静态路由的优先级[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display ip routing-table {{destination_network}}",
      "display ip routing-table {{destination_network}} verbose"
    ],
    "analysis_type": "check_priority",
    "destination_network": "<目的网段>",
    "device_info": {
      "ip": "<设备IP>",
      "port": 23,
      "protocol": "telnet",
      "username": "<用户名>",
      "password": "<密码>"
    }
  }
}
```

**等待工具返回**，然后从返回结果中提取 `analysis.next_step`：
- 如果 `next_step == "step7"` → 跳转到【第七步】

---

### **第七步：流程结束与总结**

[STEP_START]流程结束与总结[STEP_END]

**此时你可以输出少量总结文本**，例如：

```
静态路由故障排查完成。

诊断结果：<根据前面步骤的分析结果总结>

建议：<给出具体建议>
```

**如果问题仍未解决**，输出：
```
建议收集相关设备的诊断信息，并拨打技术支持热线 400-810-0504 寻求进一步帮助。
```

---

