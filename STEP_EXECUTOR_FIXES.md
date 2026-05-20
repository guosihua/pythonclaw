# Step Executor 修复摘要

## 问题分析

在执行 `execute_step_script` 工具时，出现了以下错误：

```
AttributeError: 'str' object has no attribute 'get'
```

这发生在 `step_executor.py` 的第 418 行（现在是第 427 行），位置在：
```python
commands_templates = params.get("commands", [])
```

## 根本原因

1. **参数类型不匹配**
   - LLM 生成的工具调用中，`params` 字段是一个 JSON 字符串而非对象
   - `step_executor.py` 期望 `params` 是一个字典

2. **缺少必要字段**
   - LLM 没有提供 `commands` 字段，只提供了 `step_type`
   - 没有提供 `session_id`

3. **工具定义不清晰**
   - `execute_step_script` 工具定义中没有清楚说明可选字段
   - 没有说明 `step_type` 可以用作 `analysis_type` 的别名

## 修复清单

### 1. `core/tools.py` - `execute_step_script` 函数

**修改**：添加参数类型检查
```python
def execute_step_script(script_name: str, mode: str, params: dict | str) -> str:
    # 确保 params 是字典（LLM 可能生成为 JSON 字符串）
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid JSON in params: {params}"})
```

**原因**：处理 LLM 生成的 JSON 字符串参数

### 2. `core/tools.py` - `EXECUTE_STEP_TOOL` 工具定义

**修改**：更新工具定义，明确所有字段及其用途
```python
"params": {
    "type": "object",
    "description": "Parameters for the script execution.",
    "properties": {
        "commands": {
            "type": "array",
            "description": "List of command templates. OPTIONAL if analysis_type is provided"
        },
        "analysis_type": {
            "type": "string",
            "description": "Type of analysis (check_route, check_nexthop, ...)"
        },
        "step_type": {
            "type": "string",
            "description": "Alias for analysis_type. Can be used as fallback."
        },
        "session_id": {
            "type": "string",
            "description": "Unique session ID. Auto-generated if not provided."
        },
        "destination_network": {
            "type": "string",
            "description": "Target network for route checking"
        },
        "nexthop_ip": {
            "type": "string",
            "description": "Next-hop IP address"
        },
        // ... 其他字段
    },
    "required": ["analysis_type"]  // 只有 analysis_type 是必需的
}
```

**原因**：让 LLM 知道哪些字段是必需的，哪些是可选的

### 3. `step_executor.py` - `main()` 函数

**修改 1**：参数解析错误处理
```python
params_input = sys.argv[2]
if isinstance(params_input, str):
    try:
        params = json.loads(params_input)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON in params: {e}"}))
        sys.exit(1)
else:
    params = params_input
```

**修改 2**：使用 `step_type` 作为 `analysis_type` 的别名
```python
# Determine the analysis type (use step_type as fallback)
effective_analysis_type = analysis_type or step_type
```

**修改 3**：自动生成 `commands` 和 `session_id`
```python
# Auto-generate session_id if not provided
if not session_id:
    import uuid
    session_id = f"troubleshooting-{uuid.uuid4().hex[:8]}"

# Auto-generate commands if not provided
if not commands_templates and effective_analysis_type:
    commands_templates = STEP_COMMAND_TEMPLATES.get(effective_analysis_type, [])
```

**修改 4**：更新后续代码使用 `effective_analysis_type`
```python
if effective_analysis_type not in ANALYSIS_HANDLERS:
    print(json.dumps({"error": f"Unknown analysis_type: {effective_analysis_type}..."}))
    sys.exit(1)

analyzer = ANALYSIS_HANDLERS[effective_analysis_type]
```

**原因**：
- 处理 LLM 生成的 JSON 字符串
- 支持使用 `step_type` 代替 `analysis_type`
- 自动生成缺失的必要参数
- 根据 `analysis_type`/`step_type` 自动选择命令模板

## 测试场景

### 场景 1：完整参数
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "commands": ["display ip routing-table {{destination_network}}"],
    "analysis_type": "check_route",
    "session_id": "sess-123",
    "destination_network": "192.168.1.0/24"
  }
}
```

### 场景 2：部分参数（使用 `step_type` 和自动生成）
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "step_type": "check_route",
    "destination_network": "192.168.1.0/24"
  }
}
```
- `session_id` 会被自动生成
- `commands` 会根据 `step_type` 从 `STEP_COMMAND_TEMPLATES` 自动生成

### 场景 3：使用 `analysis_type`
```json
{
  "script_name": "step_executor.py",
  "mode": "build_and_execute",
  "params": {
    "analysis_type": "check_nexthop",
    "nexthop_ip": "10.1.1.1"
  }
}
```
- `session_id` 会被自动生成
- `commands` 会自动生成

## 预期行为

修复后，`execute_step_script` 工具应该：

1. ✅ 接受 LLM 生成的 JSON 字符串或对象格式的 `params`
2. ✅ 根据 `analysis_type` 或 `step_type` 自动生成命令
3. ✅ 自动生成 `session_id` 如果未提供
4. ✅ 正确执行设备命令并分析结果
5. ✅ 返回结构化的分析结果

## 文件修改

1. `core/tools.py`
   - 修改 `execute_step_script()` 函数：添加参数解析
   - 更新 `EXECUTE_STEP_TOOL` 定义：明确字段和必需项

2. `templates/skills/troubleshooting/static-troubleshooting/step_executor.py`
   - 修改 `main()` 函数参数解析
   - 添加自动生成 `commands` 和 `session_id` 的逻辑
   - 更新 `analysis_type` 使用逻辑

