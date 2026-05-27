# 以太口故障排查技能

## 功能概述

本技能用于排查网络设备以太口故障，按照标准流程逐步定位和解决端口问题。

## 支持的设备

- H3C 系列交换机和路由器

## 故障排查流程

### 第一步：查看端口是否Down
- 查看物理端口状态
- 如果端口Up → 跳转到第三步
- 如果端口Down → 跳转到第二步

### 第二步：检查Down类型
- 识别端口Down的具体原因
- 包括：Administrative shutdown、Loopback-detection、BPDU guard等

### 第三步：检查错包增长情况
- 分析入方向和出方向的错误包统计
- 识别CRC、frame、overruns等错误

### 第四步：判断端口类型
- 识别光口或电口
- 光口 → 跳转到第七步
- 电口 → 跳转到第五步

### 第五步：检查双工速率
- 确认端口速率和双工模式配置

### 第六步：内环测试
- 在交换芯片内部建立自环测试

### 第七步：查看光功率
- 检查收发光功率是否在正常范围

### 第八步：光模块自环测试
- 使用尾纤连接光模块进行自环测试

### 第九步：检查光纤和模块
- 查看光模块信息，建议替换排查

## 使用方式

1. 激活技能：`use_skill("ethernet-port-troubleshooting")`
2. 提供设备IP和接口名称
3. 按照步骤逐步排查

## 命令说明

| 步骤 | 命令 | 说明 |
|------|------|------|
| 1-3, 5 | `display interface <interface>` | 查看接口状态和统计 |
| 4 | `display interface <interface>` | 查看介质类型 |
| 6 | `interface <interface>; loopback internal` | 配置内环测试 |
| 7 | `display transceiver diagnosis interface <interface>` | 查看光功率 |
| 9 | `display transceiver interface <interface>` | 查看光模块信息 |

## 技术支持

如无法解决问题，请拨打技术支持热线：400-810-0504
