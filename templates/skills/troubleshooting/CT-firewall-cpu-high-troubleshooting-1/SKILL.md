---
name: CT-firewall-cpu-high-troubleshooting
description: 防火墙CPU利用率高故障排查
keywords:
  - 防火墙CPU高
  - CPU利用率高
  - 防火墙性能
  - CPU进程
  - 会话统计
---

# 防火墙CPU利用率高故障排查流程

## 技能概述
- &zwnj;**名称**&zwnj;：CT-firewall-cpu-high-troubleshooting
- &zwnj;**描述**&zwnj;：针对H3C防火墙设备CPU利用率异常升高的问题，提供标准化、引导式的排查与修复流程。流程基于标准排查思路，旨在快速定位CPU占用高的原因并解决。
- &zwnj;**关键词**&zwnj;：防火墙CPU高、CPU利用率高、防火墙性能、CPU进程、会话统计

## 核心流程图（严格按步骤顺序执行）

流程图：
开始故障排查
    ↓
步骤1：故障现象确认
    ↓
步骤2：引导提供诊断信息
    ├─ 能提供 → 收集诊断信息 → 步骤4
    └─ 不能提供 → 步骤3
    ↓
步骤3：收集关键命令回显
    ↓
步骤4：执行检查点分析
    ├─ 检查点1：查看CPU利用率
    ├─ 检查点2：查看CPU进程
    ├─ 检查点3：查看多核引擎利用率
    ├─ 检查点4：查看会话统计
    └─ 检查点5：查看接口流量
    ↓
步骤5：输出分析结果
    ↓
步骤6：决策与下一步行动
    ├─ 存在未通过项 → 列出问题、提供修复建议 → 等待用户"已处理"
    │   └─ 用户回复"已处理" → 检查处理结果 → 查看CPU状态 → 继续分析
    └─ 全部通过/无异常 → 告知用户 → 提供后续建议 → 可转人工
    ↓
步骤7：用户支持与问题升级
    └─ 用户不会操作 → 联系网络工程师 → 收集信息 → 结束

## 详细步骤内容：

### 步骤1：故障现象确认

&zwnj;**若客户已主动提供以下信息，则直接跳过此步骤，进入步骤2：**&zwnj;
- 设备型号和软件版本（如 SecPath F1000-AI R6745）
- 具体的故障现象（如CPU利用率持续多高、何时开始出现、是否伴随业务异常等）

&zwnj;**若信息不完整，则询问缺失项：**&zwnj;
- 设备型号和软件版本（如 `display version` 输出）
- 请客户描述具体的故障现象（例如：CPU利用率持续在80%以上、业务访问缓慢、设备响应慢等）
- 故障开始时间、是否周期性出现、是否伴随特定业务流量

---

### 步骤2：引导客户提供诊断信息或配置文件

请客户提供以下其中一种信息：

- `display diagnostic-information` 的完整输出
- 或 `display current-configuration`的完整输出

---

### 步骤3：若无法提供步骤2的信息（诊断信息或配置文件），则提供以下关键命令的回显（一次性将下面命令提供给客户），如果可以提供步骤2的信息，则跳过步骤3：
当需要让客户执行命令时，需要直接给客户可复制的按钮，尽量复制一次就可以使用，每一条命令注意要换行（重要）



display cpu-usage
display cpu-usage task
display cpu-usage history
display firewall session statistics
display firewall session table
display interface brief
display counters rate inbound interface [interface-name]
display counters rate outbound interface [interface-name]
display qos policy interface [interface-name]
display security-policy statistics
display logbuffer
display process cpu

text

> &zwnj;**说明**&zwnj;：
> - `display cpu-usage` 用于查看当前CPU利用率。
> - `display cpu-usage task` 和 `display cpu-usage history` 用于查看CPU使用历史和任务占用。
> - `display firewall session statistics` 和 `display firewall session table` 用于查看会话统计和会话表信息。
> - `display interface brief` 和 `display counters rate` 用于查看接口状态和流量速率。
> - `display qos policy interface` 用于查看接口应用的QoS策略。
> - `display security-policy statistics` 用于查看安全策略命中统计。
> - `display logbuffer` 用于查看系统日志。
> - `display process cpu` 用于查看进程级别的CPU占用情况。

---

### 步骤4：执行检查点分析

获取诊断信息后，按以下顺序执行检查：

#### 检查点1：查看CPU利用率
- [ ] &zwnj;**当前CPU利用率**&zwnj;：执行 `display cpu-usage`，查看当前CPU利用率是多少？是否持续高位（如>70%）？
- [ ] &zwnj;**CPU使用历史**&zwnj;：执行 `display cpu-usage history`，查看CPU利用率的历史趋势图，是持续高位还是突发性高峰？
- [ ] &zwnj;**CPU占用任务**&zwnj;：执行 `display cpu-usage task`，查看哪些任务占用了较高的CPU资源？

> &zwnj;**判断标准**&zwnj;：持续高CPU利用率（>70%）通常表示存在问题，需要进一步分析。

#### 检查点2：查看CPU进程
- [ ] &zwnj;**进程级CPU占用**&zwnj;：执行 `display process cpu`，查看具体哪个进程占用了最高的CPU？
- [ ] &zwnj;**进程状态**&zwnj;：查看高CPU占用进程的状态是否正常？是否有进程异常重启或僵死？
- [ ] &zwnj;**进程功能**&zwnj;：识别高CPU进程的功能（如防火墙进程、路由进程、VPN进程等）。

> &zwnj;**常见高CPU进程**&zwnj;：`firewall`（防火墙处理）、`route`（路由计算）、`vpn`（VPN加解密）、`attack-defense`（攻击防范）等。

#### 检查点3：查看多核引擎利用率
- [ ] &zwnj;**多核CPU分布**&zwnj;：如果设备支持多核，查看各个CPU核心的利用率分布是否均衡？
- [ ] &zwnj;**引擎负载**&zwnj;：检查是否有单个CPU核心利用率特别高，而其他核心利用率较低的情况？

> &zwnj;**注意**&zwnj;：某些业务可能只由特定CPU核心处理，导致负载不均衡。

#### 检查点4：查看会话统计
- [ ] &zwnj;**会话数量**&zwnj;：执行 `display firewall session statistics`，查看当前会话总数是多少？是否超过设备规格？
- [ ] &zwnj;**会话创建速率**&zwnj;：查看会话创建速率是否异常高？
- [ ] &zwnj;**会话老化情况**&zwnj;：检查是否有大量会话长时间不老化？
- [ ] &zwnj;**会话表项分布**&zwnj;：执行 `display firewall session table`，查看会话分布是否均匀？

> &zwnj;**判断标准**&zwnj;：会话数量过多或创建速率过快都会导致CPU升高。一般防火墙会话数超过规格的70%就可能影响性能。

#### 检查点5：查看接口流量
- [ ] &zwnj;**接口流量统计**&zwnj;：执行 `display counters rate inbound interface` 和 `display counters rate outbound interface`，查看各接口的流量速率。
- [ ] &zwnj;**异常流量识别**&zwnj;：检查是否有接口流量异常高？是否有大量小包（如64字节以下）？
- [ ] &zwnj;**安全策略统计**&zwnj;：执行 `display security-policy statistics`，查看哪些安全策略被频繁匹配？
- [ ] &zwnj;**QoS策略**&zwnj;：检查是否配置了复杂的QoS策略？执行 `display qos policy interface` 查看。

> &zwnj;**关键点**&zwnj;：大量小包处理、复杂策略匹配、流量突发都可能导致CPU升高。

---

### 步骤5：输出分析结果

对每个检查点中的子项进行判断，标记结果：
- &zwnj;**通过**&zwnj;：在对应项后标记 &zwnj;**✅**&zwnj;
- &zwnj;**不通过**&zwnj;：在对应项后标记 &zwnj;**❌**&zwnj;，并说明具体问题
- 输出分析结果尽量以表格的形式呈现

&zwnj;**示例输出格式：**&zwnj;
| 检查点 | 检查项 | 结果 | 问题描述 |
| :--- | :--- | :--- | :--- |
| 检查点1 | 当前CPU利用率 | ❌ | CPU利用率持续在85%以上。 |
| 检查点1 | CPU使用历史 | ❌ | 历史显示CPU利用率从昨天开始持续高位。 |
| 检查点2 | 进程级CPU占用 | ❌ | `firewall` 进程占用CPU 65%。 |
| 检查点3 | 多核负载均衡 | ✅ | 各CPU核心负载分布均匀。 |
| 检查点4 | 会话数量 | ❌ | 当前会话数达到120万，接近设备规格上限。 |
| 检查点5 | 接口流量 | ❌ | GE1/0/1接口入方向小包（64字节）占比超过60%。 |

---

### 步骤6：决策与下一步行动

根据步骤4和步骤5的结果：

- &zwnj;**如果存在未通过项（确认或高度怀疑存在问题）**&zwnj;：
  1.  &zwnj;**列出所有发现的具体问题**&zwnj;（如上述表格）。
  2.  &zwnj;**提供修复建议与命令**&zwnj;：
      - &zwnj;**会话数过多**&zwnj;：调整会话老化时间、清理无效会话。
        ```
        # 查看并调整会话老化时间
        display firewall session aging-time
        firewall session aging-time tcp 1200
        firewall session aging-time udp 60
        #
        # 清理会话（谨慎使用）
        reset firewall session table
        #
        ```
      - &zwnj;**异常流量**&zwnj;：识别并限制异常流量。
        ```
        # 创建ACL匹配异常流量
        acl advanced 3000
        rule 0 permit ip source any destination any
        #
        # 应用QoS限速
        qos policy abnormal-traffic
        classifier abnormal behavior
        if-match acl 3000
        car cir 10000
        #
        interface GigabitEthernet 1/0/1
        qos apply policy abnormal-traffic inbound
        #
        ```
      - &zwnj;**安全策略优化**&zwnj;：优化频繁匹配的安全策略，调整规则顺序。
        ```
        # 查看策略命中统计
        display security-policy statistics
        # 将频繁匹配的规则调整到前面
        security-policy ip
        rule 0 name permit_high_traffic
        action pass
        source-zone trust
        destination-zone untrust
        source-ip-host 192.168.1.100
        #
        ```
      - &zwnj;**进程异常**&zwnj;：重启异常进程（谨慎操作）。
        ```
        # 查看进程ID
        display process cpu
        # 重启进程（如进程ID 123）
        reset process 123
        #
        ```
      - &zwnj;**设备规格不足**&zwnj;：考虑升级设备硬件或优化网络架构。
  3.  &zwnj;**要求用户处理完成后回复"已处理"**&zwnj;。

- &zwnj;**如果全部通过/未发现明确问题**&zwnj;：
  1.  告知用户基础检查未发现典型问题。
  2.  提供后续排查方向建议：
      - 使用 `debugging` 命令（谨慎使用，需技术支持指导）进一步分析。
      - 开启流量镜像，使用抓包工具分析流量特征。
      - 检查是否有网络攻击（如DDoS）导致CPU升高。
      - 联系H3C技术支持进行深度分析。
  3.  如需进一步协助，可输入"转人工"。

- &zwnj;**当用户回复"已处理"时**&zwnj;：
  1.  &zwnj;**不要重复询问客户是否已处理**&zwnj;。
  2.  &zwnj;**先检查客户处理是否正确**&zwnj;：让客户执行以下命令确认：
      - 确认CPU利用率是否下降：
        ```
        display cpu-usage
        display cpu-usage history
        #
        ```
      - 确认会话数是否减少：
        ```
        display firewall session statistics
        #
        ```
      - 确认配置已保存：
        ```
        display current-configuration | include firewall
        display current-configuration | include qos
        #
        ```
  3.  &zwnj;**验证处理效果**&zwnj;：观察一段时间（如5-10分钟）后，再次检查CPU利用率。
  4.  等待客户发送结果后再继续分析。

---

### 步骤7：用户支持与问题升级

当用户表示"不会操作"、"看不懂"或不耐烦时：

1.  暂停技术排查。
2.  说明将为用户联系网络工程师。
3.  要求提供：姓名、联系电话、设备序列号、故障现象简要描述。
4.  告知已记录提交，工程师会尽快联系。同时可提供H3C官方服务热线：&zwnj;**400-810-0504**&zwnj;。

---

## 故障总结示例

完成故障排查后，可按以下格式总结发现的问题：

> 问题已解决！总结如下：
> 1.  &zwnj;**确认CPU利用率持续高位**&zwnj;：CPU利用率持续在85%以上。
> 2.  &zwnj;**根本原因**&zwnj;：
>     - 防火墙会话数达到120万，接近设备规格上限。
>     - GE1/0/1接口入方向小包占比过高，达到60%，大量小包处理消耗CPU资源。
> 3.  &zwnj;**解决措施**&zwnj;：
>     - 调整TCP会话老化时间从默认值缩短为1200秒。
>     - 在GE1/0/1接口入方向应用QoS策略，对小包流量进行限速。
> 4.  &zwnj;**结果验证**&zwnj;：处理后CPU利用率下降至45%，会话数稳定在80万左右。

---

## 使用提示（必须遵守）

1.  本技能&zwnj;**按步骤顺序执行**&zwnj;，每步仅做该步的事情。
2.  不提前告知用户后续步骤内容。
3.  CPU高问题可能由多种原因引起，需要综合多个检查点的结果进行分析。
4.  必须等待用户对每一个步骤的所有问题都做出回答后，才能进入下一个步骤。若存在未回答的问题，需主动再次提问客户未回答的问题，只有当客户表示无法提供时，才可以跳过。
5.  给客户任何命令时，都需要注意提供可以直接复制的按钮。
6.  在建议执行 `reset firewall session table` 或 `reset process` 等可能影响业务的操作前，务必提醒用户风险，并建议在业务低峰期操作。
