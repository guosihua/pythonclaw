# Static Troubleshooting Skill - Python Scripts

This directory contains Python scripts for the static route troubleshooting skill.

## Overview

Each step in the troubleshooting process has a corresponding Python script that:
1. **Builds** the device command data structure for frontend consumption
2. **Analyzes** the device response and determines the next step

## Script Structure

### Naming Convention
- `step{number}_{description}.py`
- Example: `step1_check_route.py`, `step2_check_nexthop.py`

### Usage Modes

Each script supports two modes:

#### 1. Build Mode
Generates the command data structure to send to the frontend.

```bash
python step1_check_route.py build '{
  "destination_network": "192.168.1.0/24",
  "device_info": {
    "ip": "10.88.142.204",
    "password": "1qaz!QAZ",
    "port": 23,
    "protocol": "telnet",
    "username": "admin@local",
    "uuid": "27c2b8d0-9078-4f51-9230-afc9a614778f"
  },
  "context_id": "9735cac6-8d53-493c-bab7-c6ff8c3ee470",
  "question_no": "session_1779162176398_n7le7ta411779162183194",
  "session_id": "session_1779162176398_n7le7ta41",
  "current_step": 1
}'
```

**Output Format:**
```json
{
  "answerType": "stepCommand",
  "contextEnd": "false",
  "contextId": "9735cac6-8d53-493c-bab7-c6ff8c3ee470",
  "currentStep": 1,
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
  },
  "questionNo": "session_1779162176398_n7le7ta411779162183194",
  "sessionId": "session_1779162176398_n7le7ta41"
}
```

#### 2. Analyze Mode
Analyzes the device response and returns decision logic.

```bash
python step1_check_route.py analyze '<device response data>'
```

**Output Format:**
```json
{
  "step": 1,
  "step_name": "检查全局路由表中是否存在该静态路由",
  "status": "success",
  "route_exists": true,
  "next_step": "step2",
  "message": "路由表中存在该静态路由条目，继续检查下一跳可达性"
}
```

## Workflow

1. **Agent** calls `execute_step_script` tool with script name and parameters
2. **Script** (build mode) generates command data structure
3. **Frontend** receives the data and executes the device command
4. **Frontend** sends device response to backend analysis API
5. **Backend** calls script (analyze mode) to parse the response
6. **Agent** receives analysis result and decides next step

## Available Scripts

| Step | Script | Purpose |
|------|--------|---------|
| 1 | `step1_check_route.py` | Check if static route exists in routing table |
| 2 | `step2_check_nexthop.py` | Check next-hop IP reachability via ping |
| 3+ | (To be implemented) | Additional troubleshooting steps |

## Integration with Agent

The Agent should use the `execute_step_script` tool to invoke these scripts:

```python
# Example tool call
{
  "tool": "execute_step_script",
  "arguments": {
    "script_name": "step1_check_route.py",
    "params": {
      "destination_network": "192.168.1.0/24",
      "device_info": {...},
      "context_id": "...",
      "question_no": "...",
      "session_id": "...",
      "current_step": 1
    }
  }
}
```

## Error Handling

All scripts return JSON-formatted error messages on failure:

```json
{
  "error": "Description of the error"
}
```

Common errors:
- Missing parameters
- Invalid JSON input
- Unknown mode
- Parsing failures

## Testing

You can test each script independently:

```bash
# Test build mode
python step1_check_route.py build '{"destination_network": "192.168.1.0/24", "device_info": {"ip": "10.88.142.204", "password": "test", "port": 23, "protocol": "telnet", "username": "admin", "uuid": "test-uuid"}, "context_id": "test-context", "question_no": "test-question", "session_id": "test-session", "current_step": 1}'

# Test analyze mode
python step1_check_route.py analyze 'Routing Table: Total Destinations : 10...Static 192.168.1.0/24...'
```
