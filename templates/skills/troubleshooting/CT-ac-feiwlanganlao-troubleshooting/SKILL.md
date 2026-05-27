---
name: CT-ac-feiwlanganlao-troubleshooting
author: xms
version: 1.0
createTime: 2026-05-21
description: 遵循无线非wlan干扰问题排查，对无线干扰进行标准化排查
scene: 无线非WLAN干扰问题排查
keywords:
  - 无线干扰
  - wlan干扰
  - 无线空口大
  - 无线利用率高
  - 无线干扰
output: 逐项检查 + ✅正常/❌异常标记 + 统一异常整改
transferRule: 对话>5轮 或 用户转人工 → 收集姓名、联系方式、设备序列号
---

# CT-ac-feiwlanganlao-troubleshooting
## 基本信息
- 技能名称：CT-ac-feiwlanganlao-troubleshooting
- 作者：xms
- 故障场景：无线非WLAN干扰问题排查
- 适用设备：H3C AP
- 排查频段：2.4G
# 无线非WLAN干扰问题排查 Skill
## 一、排查思路
先检查AP空口利用率是否具备非WLAN干扰特征，再通过滤波器、频谱分析仪、现场环境检查验证。

## 二、排查步骤与命令
### 1. 查看AP 2.4G空口利用率
- 进入隐藏模式：`[H3C] probe
- 查看命令：`[H3C-probe] display ar5 2 channelbusy`
- 检查项：CtlBusy、TxBusy、RxBusy、ExtBusy

### 2. 空口利用率判断
- 规则：CtlBusy > 60% → ❌异常；≤60% → ✅正常

### 3. WLAN同频干扰判断
- 规则：CtlBusy > 60% 且 RxBusy > 60% → ❌异常
- 处理：按《无线通用优化规范》优化

### 4. 非WLAN干扰判断
- 规则：CtlBusy − (TxBusy + RxBusy) > 30% → ❌异常

### 5. 非WLAN干扰验证（三选一）
1. 频谱分析仪：扫描1.8G–2.4G，存在强电磁波 → ❌异常
2. 滤波器：加装后空口利用率明显下降 → ❌异常
3. 现场检查：微波炉、运营商4G天线等 → ❌异常

## 三、结果输出规范
- 每项检查标注 **✅正常 / ❌异常**
- 汇总所有异常项并给出整改建议

## 四、转人工流程
请用户提供：
1. 客户姓名
2. 联系方式
3. 设备序列号