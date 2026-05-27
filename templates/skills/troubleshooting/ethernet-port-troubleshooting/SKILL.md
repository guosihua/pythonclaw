---
name: ethernet-port-troubleshooting
description: 以太口故障排查，用于排查网络设备以太口故障的标准流程，包含端口状态检查、错包分析、光模块检测等步骤
fetch_topology: false
trigger:
  type: keyword
  keywords:
    - 以太口故障排查
    - 端口Down
    - 端口不通
    - 接口故障
---

# 自动化脚本执行器 - 以太口故障排查

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

从用户输入或上下文中提取以下参数（必须由用户提供）：

| 参数 | 提取方式 | 说明 |
|------|---------|------|
| `interface_name` | 通过追问或者问题中提取接口名称 | 必须提供，无默认值 |

---

### **支持的 analysis_type 值**：

| analysis_type | 对应步骤 | 说明 |
|-----------|---------|------|
| `check_port_status` | Step 1 | 查看端口是否Down |
| `check_down_type` | Step 2 | 检查Down类型 |
| `check_error_packets` | Step 3 | 检查错包增长情况 |
| `check_port_type` | Step 4 | 端口是否是光口 |
| `check_duplex_speed` | Step 5 | 检查双工速率 |
| `loopback_test` | Step 6 | 内环测试 |
| `check_optical_power` | Step 7 | 查看两端收发光功率 |
| `optical_loopback_test` | Step 8 | 光模块自环测试 |
| `check_fiber_module` | Step 9 | 检查光纤、替换模块 |

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
            "command": "display interface GigabitEthernet 1/0/1",
            "echo": "display interface GigabitEthernet 1/0/1\r\n...回显文本..."
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

### **第一步：查看端口是否Down**

[STEP_START]查看端口是否Down[STEP_END]

**立即执行**：调用 `execute_step_script` 工具
**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display interface {{interface_name}}"
    ],
    "analysis_type": "check_port_status",
    "interface_name": "<从上下文提取的接口名称，默认 GigabitEthernet 1/0/1>",
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
- 如果 `next_step == "step3"` → 跳转到【第三步】

---
### **第二步：检查Down类型**

[STEP_START]检查Down类型[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display interface {{interface_name}}"
    ],
    "analysis_type": "check_down_type",
    "interface_name": "<接口名称>",
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
- 如果 `next_step == "step4"` → 跳转到【第四步】

---

### **第三步：检查错包增长情况**

[STEP_START]检查错包增长情况[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display interface {{interface_name}}"
    ],
    "analysis_type": "check_error_packets",
    "interface_name": "<接口名称>",
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
- 如果 `next_step == "step4"` → 跳转到【第四步】

---

### **第四步：端口是否是光口**

[STEP_START]端口是否是光口[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display interface {{interface_name}}"
    ],
    "analysis_type": "check_port_type",
    "interface_name": "<接口名称>",
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

### **第五步：检查双工速率**

[STEP_START]检查双工速率[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display interface {{interface_name}}"
    ],
    "analysis_type": "check_duplex_speed",
    "interface_name": "<接口名称>",
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

---

### **第六步：内环测试**

[STEP_START]内环测试[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "interface {{interface_name}}",
      "loopback internal",
      "display interface {{interface_name}}"
    ],
    "analysis_type": "loopback_test",
    "interface_name": "<接口名称>",
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
- 如果 `next_step == "step9"` → 跳转到【第九步】
- 如果 `next_step == "finish"` → 故障未解决，建议拨打热线

---

### **第七步：查看两端收发光功率**

[STEP_START]查看两端收发光功率[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display transceiver diagnosis interface {{interface_name}}"
    ],
    "analysis_type": "check_optical_power",
    "interface_name": "<接口名称>",
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
- 如果 `next_step == "step8"` → 执行【第八步】

---

### **第八步：光模块自环测试**

[STEP_START]光模块自环测试[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display interface {{interface_name}}"
    ],
    "analysis_type": "optical_loopback_test",
    "interface_name": "<接口名称>",
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
- 如果 `next_step == "step9"` → 跳转到【第九步】

---

### **第九步：检查光纤、替换模块**

[STEP_START]检查光纤、替换模块[STEP_END]

**立即执行**：调用 `execute_step_script` 工具

**工具参数**：
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": [
      "display transceiver interface {{interface_name}}"
    ],
    "analysis_type": "check_fiber_module",
    "interface_name": "<接口名称>",
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
- 如果 `next_step == "finish"` → 故障排查流程结束

---

## 故障排查流程图

```
第一步：查看端口是否Down
        ↓
   ┌────┴────┐
   │         │
  Up      Down
   │         │
   ↓         ↓
第三步    第二步
检查错包   检查Down类型
增长情况
   │         │
   └────┬────┘
        ↓
   第四步：端口是否是光口
        ↓
   ┌────┴────┐
   │         │
 光口      电口
   │         │
   ↓         ↓
 第七步    第五步
查看收     检查双工
发光功率   速率
   │         │
   ↓         ↓
 第八步    第六步
光模块     内环测试
自环测试
   │         │
   └────┬────┘
        ↓
   第九步：检查光纤、替换模块
        ↓
    完成/拨打热线
```
