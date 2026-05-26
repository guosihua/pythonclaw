---
name: network-topology
description: 网络拓扑图获取，用于获取整网拓扑结构并展示
fetch_topology: false
trigger:
  type: keyword
  keywords:
    - 查看拓扑图
    - 获取网络拓扑
    - 网络拓扑
---

# 网络拓扑图获取技能

## 功能说明

本技能用于获取网络设备的拓扑结构，支持独立调用或作为其他故障排查技能的前置步骤。

## 执行流程

```
激活技能 (use_skill)
  ↓
调用拓扑接口获取设备列表和连接关系
  ↓
发送拓扑数据给前端展示
```

## 工具调用

**工具名称**：`execute_topology_script`

**工具参数**：
```json
{
  "params": {
    "sessionId": "<会话ID>",
    "contextId": "<上下文ID>",
    "questionNo": "<问题编号>"
  }
}
```

## 底层行为

1. 加载 `file/devices.json` 中所有设备信息
2. 调用第三方接口 `POST http://127.0.0.1:20160/api/deviceTopology/get`
3. 将响应中的 `nodes` 和 `edges` 数据发送给前端

## 发送给前端的消息

```json
{"answerType":"conversation","contextEnd":"false","contextId":"<contextId>","currentStep":0,"message":"网络拓扑图获取中...<br/>","questionNo":"<questionNo>","sessionId":"<sessionId>"}
```

```json
{"answerType":"topology","contextEnd":"false","contextId":"<contextId>","currentStep":0,"message":{"edges":[...],"nodes":[...]},"questionNo":"<questionNo>","sessionId":"<sessionId>"}
```

```json
{"answerType":"conversation","contextEnd":"false","contextId":"<contextId>","currentStep":0,"message":"获取成功","questionNo":"<questionNo>","sessionId":"<sessionId>"}
```
