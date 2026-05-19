# 前端集成指南

本文档描述了如何将基于步骤的故障排查工作流与您的前端应用程序集成。

## 架构概览

```
┌─────────────┐         ┌──────────────┐         ┌─────────────┐
│   前端      │◄────────│   后端       │◄────────│   Agent     │
│             │         │              │         │             │
│ • UI 显示   │         │ • 工具调用   │         │ • LLM 逻辑  │
│ • 设备执行  │         │ • 脚本执行   │         │ • 步骤管理  │
│ • 分析请求  │         │ • 分析接口   │         │             │
└──────┬──────┘         └──────┬───────┘         └─────────────┘
       │                       │
       │                       │
       ▼                       ▼
┌─────────────┐         ┌──────────────┐
│   网络设备  │         │ Python 脚本  │
│             │         │              │
│ • Telnet    │         │ • Build 模式 │
│ • SSH       │         │ • Analyze 模式│
│ • SNMP      │         │              │
└─────────────┘         └──────────────┘
```

**核心职责划分**：

**后端**：
- 提供 `execute_step_script` 工具供 Agent 调用
- 执行 Python 脚本（build/analyze 两种模式）
- 提供 `/api/step/analyze` 分析接口
- 返回结构化的命令数据给前端

**前端**：
- 接收 `stepCommand` 消息
- **自行实现设备命令执行**（Telnet/SSH/SNMP 等协议）
- 收集设备回显数据
- 调用分析接口获取下一步决策
- 根据分析结果更新 UI 并继续流程

**Python 脚本**：
- Build 模式：生成符合前端要求的命令数据结构
- Analyze 模式：解析设备回显并决定下一步操作

## 消息处理

### 1. 步骤通知处理器

当接收到步骤通知消息时：

```javascript
// 示例：WebSocket 或 SSE 消息处理器
function handleMessage(message) {
    const data = JSON.parse(message.data);
    
    if (data.answerType === 'stepName') {
        // 更新 UI 显示当前步骤
        updateStepIndicator({
            stepNumber: data.currentStep,
            stepName: data.message,
            sessionId: data.sessionId,
            contextId: data.contextId
        });
    }
}

function updateStepIndicator(stepInfo) {
    // 更新您的 UI 组件
    document.getElementById('current-step').textContent = 
        `步骤 ${stepInfo.stepNumber}: ${stepInfo.stepName}`;
    
    // 可选：添加到步骤历史
    addToStepHistory(stepInfo);
}
```

### 2. 步骤命令处理器

当接收到步骤命令消息时：

```javascript
function handleMessage(message) {
    const data = JSON.parse(message.data);
    
    if (data.answerType === 'stepCommand') {
        // 执行设备命令
        executeDeviceCommands(data.message.deviceCommds, data);
    }
}

async function executeDeviceCommands(deviceCommands, originalMessage) {
    try {
        // 显示加载指示器
        showLoading('正在执行设备命令...');
        
        // 执行每个命令
        const results = [];
        for (const cmd of deviceCommands) {
            const result = await executeSingleCommand(cmd);
            results.push(result);
        }
        
        // 将结果发送到后端进行分析
        const analysisResult = await sendToAnalysisEndpoint(
            results,
            originalMessage
        );
        
        // 处理分析结果
        handleAnalysisResult(analysisResult);
        
    } catch (error) {
        console.error('命令执行失败:', error);
        showError('执行设备命令失败');
    } finally {
        hideLoading();
    }
}
```

### 3. 设备命令执行

**重要提示**：设备命令执行由前端负责，而非后端。

前端接收到的命令数据格式如下：
```json
{
  "answerType": "stepCommand",
  "message": {
    "deviceCommds": [
      {
        "command": ["display ip routing-table 192.168.1.0/24"],
        "device": {
          "ip": "10.88.142.204",
          "password": "1qaz!QAZ",
          "port": 23,
          "protocol": "telnet",
          "username": "admin@local",
          "uuid": "27c2b8d0-9078-4f51-9230-afc9a614778f"
        }
      }
    ]
  }
}
```

您的前端应该使用适当的库来实现设备通信：

```javascript
// 示例：使用 Telnet 库（您需要选择合适的库）
async function executeSingleCommand(commandData) {
    const { command, device } = commandData;
    
    // 实现您自己的设备连接逻辑
    // 可以使用 Telnet、SSH、SNMP 或任何协议
    let output = '';
    
    if (device.protocol === 'telnet') {
        // 使用 Telnet 客户端库
        output = await telnetExecute({
            host: device.ip,
            port: device.port,
            username: device.username,
            password: device.password,
            commands: command
        });
    } else if (device.protocol === 'ssh') {
        // 使用 SSH 客户端库
        output = await sshExecute({
            host: device.ip,
            port: device.port || 22,
            username: device.username,
            password: device.password,
            commands: command
        });
    }
    
    return { output, success: true };
}
```

**关键点**：
- 后端**不提供** `/api/device/execute` 接口
- 前端必须自行实现设备通信逻辑
- 可以使用 `telnet-client`、`node-ssh` 等库，或自定义 WebSocket 连接
- 命令数据结构提供了所有必要的连接参数

### 4. 发送数据到分析接口

执行设备命令后，将回显数据发送到后端分析接口：

```javascript
async function sendToAnalysisEndpoint(deviceResults, originalMessage) {
    // 从设备结果中提取响应数据
    const responseData = deviceResults.map(r => r.output).join('\n');
    
    // 根据当前步骤确定使用哪个脚本
    const scriptName = determineScriptName(originalMessage.currentStep);
    
    // 调用分析接口
    const response = await fetch('/api/step/analyze', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            script_name: scriptName,
            response_data: responseData,
            session_id: originalMessage.sessionId
        })
    });
    
    if (!response.ok) {
        throw new Error(`分析失败: ${response.statusText}`);
    }
    
    return await response.json();
}

function determineScriptName(currentStep) {
    // 将步骤号映射到脚本名称
    const scriptMap = {
        1: 'step1_check_route.py',
        2: 'step2_check_nexthop.py',
        3: 'step3_check_mask.py',
        // 根据需要添加更多映射
    };
    
    return scriptMap[currentStep] || null;
}
```

**工作流程**：
1. 前端从后端接收 `stepCommand` 消息
2. 前端使用自己的实现执行设备命令
3. 前端收集设备回显数据
4. 前端调用 `/api/step/analyze` 接口发送回显数据
5. 后端分析数据并返回决策逻辑
6. 前端根据分析结果继续工作流

### 5. 处理分析结果

处理分析结果并继续工作流：

```javascript
function handleAnalysisResult(analysisResponse) {
    if (!analysisResponse.ok) {
        showError(`分析失败: ${analysisResponse.error}`);
        return;
    }
    
    const result = analysisResponse.result;
    
    // 向用户显示分析结果
    displayAnalysisResult(result);
    
    // 根据结果确定下一步操作
    if (result.next_step === 'retry') {
        // 重试当前步骤
        retryCurrentStep();
    } else if (result.next_step === 'step8') {
        // 故障排查结束
        showTroubleshootingComplete(result.message);
    } else {
        // 继续到下一步 - 向后端发送消息
        continueToNextStep(result.next_step, result.message);
    }
}

function displayAnalysisResult(result) {
    // 用分析结果更新 UI
    const resultDiv = document.getElementById('analysis-result');
    resultDiv.innerHTML = `
        <div class="step-result">
            <h4>${result.step_name}</h4>
            <p>状态: ${result.status}</p>
            <p>${result.message}</p>
        </div>
    `;
}

function continueToNextStep(nextStep, message) {
    // 向后端发送消息以继续
    sendMessageToBackend({
        type: 'continue',
        next_step: nextStep,
        analysis_result: message
    });
}
```

## 完整示例

以下是使用 WebSocket 的完整示例：

```javascript
class TroubleshootingClient {
    constructor(wsUrl) {
        this.ws = new WebSocket(wsUrl);
        this.setupEventListeners();
    }
    
    setupEventListeners() {
        this.ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            this.handleMessage(message);
        };
        
        this.ws.onerror = (error) => {
            console.error('WebSocket 错误:', error);
        };
    }
    
    async handleMessage(message) {
        switch (message.type) {
            case 'stream':
                // 处理流式文本
                this.appendText(message.content);
                break;
                
            case 'response':
                // 处理最终响应
                this.showFinalResponse(message.content);
                break;
                
            default:
                // 尝试解析为 SSE 格式
                if (message.data) {
                    const data = JSON.parse(message.data);
                    await this.handleSSEMessage(data);
                }
        }
    }
    
    async handleSSEMessage(data) {
        if (data.answerType === 'stepName') {
            // 步骤通知
            this.updateStepIndicator(data);
            
        } else if (data.answerType === 'stepCommand') {
            // 执行命令并获取分析
            await this.executeAndAnalyze(data);
        }
    }
    
    async executeAndAnalyze(stepCommandData) {
        try {
            // 执行设备命令
            const deviceResults = await this.executeCommands(
                stepCommandData.message.deviceCommds
            );
            
            // 获取分析结果
            const analysisResult = await this.analyzeResponse(
                deviceResults,
                stepCommandData
            );
            
            // 处理结果
            this.handleAnalysisResult(analysisResult);
            
        } catch (error) {
            console.error('执行失败:', error);
            this.showError(error.message);
        }
    }
    
    async executeCommands(deviceCommands) {
        const results = [];
        
        for (const cmd of deviceCommands) {
            // 前端实现自己的设备执行逻辑
            const output = await this.executeDeviceCommand(cmd);
            results.push({ output, success: true });
        }
        
        return results;
    }
    
    async executeDeviceCommand(commandData) {
        // 在此处实现您自己的设备通信逻辑
        // 这是前端的职责，不是后端的
        
        const { command, device } = commandData;
        
        // 示例：使用 Telnet/SSH 库或自定义实现
        if (device.protocol === 'telnet') {
            return await this.telnetExecute(device, command);
        } else if (device.protocol === 'ssh') {
            return await this.sshExecute(device, command);
        }
        
        throw new Error(`不支持的协议: ${device.protocol}`);
    }
    
    telnetExecute(device, commands) {
        // 实现 Telnet 执行
        // 可以使用 Node.js 的 'telnet-client' 等库
        // 或实现基于 WebSocket 的自定义终端
        console.log('通过 Telnet 执行:', commands);
        return '模拟输出...';
    }
    
    sshExecute(device, commands) {
        // 实现 SSH 执行
        // 可以使用 Node.js 的 'node-ssh' 等库
        console.log('通过 SSH 执行:', commands);
        return '模拟输出...';
    }
    
    async analyzeResponse(deviceResults, originalData) {
        const responseData = deviceResults.map(r => r.output).join('\n');
        const scriptName = this.getScriptForStep(originalData.currentStep);
        
        const response = await fetch('/api/step/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                script_name: scriptName,
                response_data: responseData,
                session_id: originalData.sessionId
            })
        });
        
        if (!response.ok) {
            throw new Error(`分析失败: ${response.statusText}`);
        }
        
        return await response.json();
    }
    
    getScriptForStep(stepNumber) {
        const scripts = {
            1: 'step1_check_route.py',
            2: 'step2_check_nexthop.py',
            3: 'step3_check_mask.py',
            4: 'step4_check_interface.py',
            5: 'step5_check_config.py',
            6: 'step6_check_bfd.py',
            7: 'step7_check_priority.py'
        };
        
        return scripts[stepNumber];
    }
    
    sendMessageToBackend(message) {
        this.ws.send(JSON.stringify(message));
    }
    
    // UI 更新方法
    updateStepIndicator(data) {
        console.log(`步骤 ${data.currentStep}: ${data.message}`);
        // 在此处更新您的 UI
    }
    
    appendText(text) {
        // 将流式文本追加到聊天
        console.log(text);
    }
    
    showFinalResponse(content) {
        // 显示最终响应
        console.log('最终:', content);
    }
    
    showError(message) {
        console.error('错误:', message);
        // 在 UI 中显示错误
    }
    
    handleAnalysisResult(result) {
        console.log('分析结果:', result);
        // 根据结果继续工作流
    }
}

// 使用示例
const client = new TroubleshootingClient('ws://localhost:8000/ws/chat');
```

## API 接口汇总

| 接口 | 方法 | 用途 |
|------|------|------|
| `/ws/chat` | WebSocket | 与 Agent 实时聊天 |
| `/chatbot/dm/claw/stream` | POST | 基于 SSE 的聊天流 |
| `/api/step/analyze` | POST | 分析设备回显数据 |

**注意**：设备命令执行完全由前端处理。后端仅提供：
1. 通过 `stepCommand` 消息提供命令数据结构
2. 分析接口用于处理设备响应

前端应根据接收到的命令数据实现自己的设备通信逻辑（Telnet/SSH/SNMP 等）。

## 错误处理

始终实现适当的错误处理：

```javascript
try {
    // 您的代码
} catch (error) {
    if (error.response) {
        // 服务器返回错误响应
        console.error('服务器错误:', error.response.data);
    } else if (error.request) {
        // 已发出请求但未收到响应
        console.error('未收到响应');
    } else {
        // 发生了其他错误
        console.error('错误:', error.message);
    }
}
```

## 测试

分别测试每个组件：

1. **步骤通知**：验证接收步骤标记时 UI 是否正确更新
2. **命令执行**：独立测试设备命令执行
3. **分析**：使用示例数据测试分析接口
4. **完整流程**：测试完整的故障排查工作流

```javascript
// 测试分析接口
async function testAnalysis() {
    const testData = {
        script_name: 'step1_check_route.py',
        response_data: 'Routing Table: Total Destinations : 10...\nStatic 192.168.1.0/24...',
        session_id: 'test-session'
    };
    
    const result = await fetch('/api/step/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(testData)
    });
    
    console.log('测试结果:', await result.json());
}
```

## 前端实现建议

前端可以使用以下库来实现设备通信：

**Node.js**:
- Telnet: `telnet-client`, `node-telnet-client`
- SSH: `node-ssh`, `ssh2`
- WebSocket 终端: `xterm.js` + 自定义后端

**浏览器**:
- 基于 WebSocket 的终端模拟器
- 通过代理连接到后端进行实际设备连接（如需要）
- 或直接实现浏览器到设备的通信

## 关键优势

1. **解耦设计**：后端不依赖任何特定的设备通信协议
2. **灵活性**：前端可以根据需要选择 Telnet、SSH、SNMP 或其他协议
3. **可扩展性**：新增设备类型只需前端实现，无需修改后端
4. **安全性**：设备凭证由前端管理，后端只传递结构化数据