"""
Unified Step Executor for CT-ac-feiwlanganlao-troubleshooting

This script auto-generates commands and analysis logic from SKILL.md.
It dynamically parses skill definitions to execute troubleshooting steps.

Usage:
    python step_executor.py build_and_execute '{"step_name": "查看AP 2.4G空口利用率", ...}'
"""

import json
import re
import sys
import os
import requests
from pathlib import Path
from typing import Dict, Any, Optional, List


TERMINAL_API_BASE_URL = "http://10.153.61.64/terminal/api/terminal/ai"
DEVICE_INFO_ENDPOINT = f"{TERMINAL_API_BASE_URL}/deviceInfo"


def get_skill_md_path() -> Path:
    return Path(__file__).parent / "SKILL.md"


def load_skill_instructions() -> str:
    skill_md_path = get_skill_md_path()
    print(f"# [DEBUG] Loading SKILL.md from: {skill_md_path}", file=sys.stderr)
    with open(skill_md_path, "r", encoding="utf-8") as f:
        return f.read()


def parse_skill_steps(instructions: str) -> List[Dict[str, Any]]:
    steps = []
    # Support multiple formats:
    # 1. ### **第1步：查看AP 2.4G空口利用率
    # 2. ### 1. 查看AP 2.4G空口利用率
    # 3. ### **第1步 查看AP 2.4G空口利用率
    step_pattern = r'###\s*\*?\*?(?:第(\d+)步：?\s*|(\d+)\.\s*)([^\n]+?)\s*\n(?:\[STEP_START\]([^\[]+)\[STEP_END\]\s*)?'
    matches = list(re.finditer(step_pattern, instructions))

    for match in matches:
        # Group 1: 第1步 format (e.g., "1")
        # Group 2: 1. format (e.g., "1")  
        # Group 3: step title
        # Group 4: step name from [STEP_START]
        step_num_str = match.group(1) or match.group(2)
        if not step_num_str:
            continue
        step_num = int(step_num_str)
        step_title = match.group(3).strip()
        step_name = match.group(4).strip() if match.group(4) else step_title

        section_start = match.end()
        section_end = len(instructions)
        for other_match in matches:
            if other_match.start() > match.start():
                section_end = other_match.start()
                break

        section_text = instructions[section_start:section_end]

        commands = []
        # Match commands with or without [H3C] prefix
        # Pattern 1: `system-view` - normal command
        # Pattern 2: `[H3C] probe` - command with prefix
        cmd_patterns = [
            r'`([^`\n]+)`',  # Match any command in backticks (including system-view)
        ]

        for pattern in cmd_patterns:
            for cmd_match in re.finditer(pattern, section_text):
                cmd = cmd_match.group(1).strip()
                # Remove [H3C] or [H3C-probe] prefix if present
                cmd = re.sub(r'^\[H3C[^\]]*\]\s*', '', cmd)
                # Clean up the command - remove any trailing newlines
                cmd = cmd.split('\n')[0].strip()
                # Filter out tool invocation commands like execute_step_script
                if cmd.lower() == 'execute_step_script':
                    continue
                if cmd and cmd not in commands:
                    commands.append(cmd)

        rules = []
        rule_patterns = [
            r'([^<*\n]+?)\s*→\s*[✅❌]\s*异常',
            r'([^<*\n]+?)\s*→\s*[✅❌]\s*正常',
            r'规则[：:]\s*([^\n]+)',
        ]
        for pattern in rule_patterns:
            for rule_match in re.finditer(pattern, section_text):
                rule = rule_match.group(1).strip()
                if rule and rule not in rules:
                    rules.append(rule)

        steps.append({
            "step_number": step_num,
            "step_name": step_name,
            "title": step_title,
            "commands": commands,
            "rules": rules,
            "raw_text": section_text[:500]
        })

    print(f"# [DEBUG] Parsed {len(steps)} steps from SKILL.md", file=sys.stderr)
    for s in steps:
        print(f"#   Step {s['step_number']}: {s['step_name']} - commands: {s['commands']}", file=sys.stderr)
    return steps


def extract_channelbusy_values(response_data: str) -> Dict[str, int]:
    values = {}
    patterns = {
        'CtlBusy': r'CtlBusy\s*[:：]\s*(\d+)%?',
        'TxBusy': r'TxBusy\s*[:：]\s*(\d+)%?',
        'RxBusy': r'RxBusy\s*[:：]\s*(\d+)%?',
        'ExtBusy': r'ExtBusy\s*[:：]\s*(\d+)%?',
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, response_data, re.IGNORECASE)
        if match:
            values[key] = int(match.group(1))

    return values


def analyze_step_result(step: Dict[str, Any], response_data: str) -> Dict[str, Any]:
    step_num = step['step_number']
    step_name = step['step_name']
    rules = step['rules']

    result = {
        "step": step_num,
        "step_name": step_name,
        "status": "normal",
        "message": "",
        "next_step": "",
        "details": {}
    }

    channelbusy = extract_channelbusy_values(response_data)
    print(f"# [DEBUG] Extracted channelbusy values: {channelbusy}", file=sys.stderr)

    if step_num == 1:
        if channelbusy:
            result["status"] = "normal"
            result["message"] = f"已获取空口利用率数据：CtlBusy={channelbusy.get('CtlBusy', 0)}%, TxBusy={channelbusy.get('TxBusy', 0)}%, RxBusy={channelbusy.get('RxBusy', 0)}%, ExtBusy={channelbusy.get('ExtBusy', 0)}%"
            result["details"] = channelbusy
            result["next_step"] = "step2"
        else:
            result["status"] = "unknown"
            result["message"] = "无法从命令输出中提取空口利用率数据"
            result["next_step"] = "finish"

    elif step_num == 2:
        ctl = channelbusy.get('CtlBusy', 0)
        result["details"]["CtlBusy"] = ctl

        if ctl > 60:
            result["status"] = "abnormal"
            result["message"] = f"CtlBusy = {ctl}% > 60%，空口利用率异常"
            result["next_step"] = "step3"
        else:
            result["status"] = "normal"
            result["message"] = f"CtlBusy = {ctl}% ≤ 60%，空口利用率正常"
            result["next_step"] = "finish"

    elif step_num == 3:
        ctl = channelbusy.get('CtlBusy', 0)
        rx = channelbusy.get('RxBusy', 0)
        result["details"]["CtlBusy"] = ctl
        result["details"]["RxBusy"] = rx

        if ctl > 60 and rx > 60:
            result["status"] = "abnormal"
            result["message"] = f"CtlBusy = {ctl}% > 60% 且 RxBusy = {rx}% > 60%，存在WLAN同频干扰"
            result["next_step"] = "step4"
        else:
            result["status"] = "normal"
            result["message"] = f"未检测到明显WLAN同频干扰（CtlBusy={ctl}%, RxBusy={rx}%）"
            result["next_step"] = "step4"

    elif step_num == 4:
        ctl = channelbusy.get('CtlBusy', 0)
        tx = channelbusy.get('TxBusy', 0)
        rx = channelbusy.get('RxBusy', 0)
        result["details"]["CtlBusy"] = ctl
        result["details"]["TxBusy"] = tx
        result["details"]["RxBusy"] = rx

        interference = ctl - (tx + rx)
        result["details"]["non_wlan_interference"] = interference

        if interference > 30:
            result["status"] = "abnormal"
            result["message"] = f"CtlBusy - (TxBusy + RxBusy) = {interference}% > 30%，存在非WLAN干扰"
            result["next_step"] = "step5"
        else:
            result["status"] = "normal"
            result["message"] = f"CtlBusy - (TxBusy + RxBusy) = {interference}% ≤ 30%，未检测到明显非WLAN干扰"
            result["next_step"] = "finish"

    elif step_num == 5:
        response_lower = response_data.lower()
        if any(kw in response_data for kw in ["强电磁波", "微波炉", "4G天线", "运营商"]):
            result["status"] = "abnormal"
            result["message"] = "检测到非WLAN干扰源（微波炉/运营商天线等）"
        elif "滤波器" in response_data or "下降" in response_data:
            result["status"] = "abnormal"
            result["message"] = "加装滤波器后空口利用率下降，确认存在非WLAN干扰"
        else:
            result["status"] = "unknown"
            result["message"] = "请提供验证结果（频谱分析仪扫描、滤波器测试或现场检查情况）"
        result["next_step"] = "finish"

    else:
        result["status"] = "unknown"
        result["message"] = f"步骤 '{step_name}' 未定义分析逻辑"
        result["next_step"] = "finish"

    return result


def load_devices_config() -> list:
    possible_paths = [
        Path(__file__).parent.parent.parent.parent.parent / "file" / "devices.json",
        Path("/config/file/devices.json"),
        Path("f:/workspace/project/H3C/pythonclaw/file/devices.json"),
    ]

    devices_path = None
    for p in possible_paths:
        if p.exists():
            devices_path = p
            break

    if devices_path is None:
        raise FileNotFoundError(f"Devices config file not found")

    with open(devices_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def execute_device_command(
    commands: List[str],
    device_info: Dict[str, Any],
    session_id: str
) -> Dict[str, Any]:
    payload = [{
        "command": commands,
        "device": {
            "ip": device_info.get("ip", ""),
            "password": device_info.get("password", ""),
            "port": device_info.get("port", 23),
            "username": device_info.get("username", ""),
            "uuid": device_info.get("uuid", ""),
            "protocol": device_info.get("protocol", "telnet")
        },
        "sessionId": session_id,
        "contextId": "",
        "questionNo": ""
    }]

    print(f"# [DEBUG] Executing commands on device: {device_info.get('ip')}", file=sys.stderr)
    print(f"# [DEBUG] Commands: {commands}", file=sys.stderr)

    try:
        response = requests.post(
            DEVICE_INFO_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        response.raise_for_status()
        result = response.json()
        print(f"# [DEBUG] API Response: {str(result)[:500]}", file=sys.stderr)
        return result
    except requests.exceptions.RequestException as e:
        print(f"# [ERROR] API request failed: {e}", file=sys.stderr)
        return {"error": str(e)}


def extract_command_output(api_result: Dict[str, Any]) -> str:
    """Extract the actual command output from API response."""
    try:
        if not isinstance(api_result, dict):
            return ""
        
        # Check for error response
        if "error" in api_result:
            return str(api_result.get("error", ""))
        
        # Try multiple possible response formats
        # Format 1: {"data": [{"echo": {"command": "output"}, ...}]}
        if "data" in api_result:
            data = api_result["data"]
            if isinstance(data, list) and len(data) > 0:
                first_item = data[0]
                # Check for echo field
                if isinstance(first_item, dict) and "echo" in first_item:
                    echo_data = first_item["echo"]
                    if isinstance(echo_data, dict) and echo_data:
                        # Return the first command output
                        return list(echo_data.values())[0]
                    elif isinstance(echo_data, str):
                        return echo_data
                # Fallback to response field or string representation
                return first_item.get("response", "") or str(first_item)
            elif isinstance(data, dict):
                return str(data)
        
        # Format 2: Direct echo field at top level
        if "echo" in api_result:
            echo_data = api_result["echo"]
            if isinstance(echo_data, dict) and echo_data:
                # Return the first command output
                return list(echo_data.values())[0]
            elif isinstance(echo_data, str):
                return echo_data
        
        # Format 3: Try to find any string output
        for key, value in api_result.items():
            if isinstance(value, str) and value.strip():
                return value
        
        return str(api_result)
        
    except Exception as e:
        print(f"# [WARNING] Failed to extract command output: {e}", file=sys.stderr)
        return ""


def build_api_responses_from_result(api_result: Dict[str, Any], commands: List[str]) -> List[Dict[str, Any]]:
    """Build api_responses list from API result, matching commands with outputs."""
    api_responses = []
    
    if not isinstance(api_result, dict) or not commands:
        return api_responses
    
    try:
        # Extract all echo data from API response
        echo_map = {}
        
        # Format 1: {"data": [{"echo": {"command": "output"}, ...}]}
        if "data" in api_result:
            data = api_result["data"]
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "echo" in item:
                        echo_data = item["echo"]
                        if isinstance(echo_data, dict):
                            echo_map.update(echo_data)
                        elif isinstance(echo_data, str):
                            # If echo is a string, associate it with the first command
                            if commands and "_raw_" not in echo_map:
                                echo_map["_raw_"] = echo_data
            elif isinstance(data, dict) and "echo" in data:
                echo_data = data["echo"]
                if isinstance(echo_data, dict):
                    echo_map.update(echo_data)
                elif isinstance(echo_data, str):
                    if commands and "_raw_" not in echo_map:
                        echo_map["_raw_"] = echo_data
        
        # Format 2: Direct echo field at top level
        if "echo" in api_result and not echo_map:
            echo_data = api_result["echo"]
            if isinstance(echo_data, dict):
                echo_map.update(echo_data)
            elif isinstance(echo_data, str):
                if commands and "_raw_" not in echo_map:
                    echo_map["_raw_"] = echo_data
        
        # If no echo data found, try to extract from response field
        if not echo_map and "data" in api_result:
            data = api_result["data"]
            if isinstance(data, list) and len(data) > 0:
                for i, item in enumerate(data):
                    if isinstance(item, dict):
                        # Try response field
                        if "response" in item and item["response"]:
                            echo_map[f"_response_{i}"] = item["response"]
                        else:
                            # Use string representation
                            echo_map[f"_item_{i}"] = str(item)
        
        # Debug: print echo map
        print(f"# [DEBUG] Echo map keys: {list(echo_map.keys())}", file=sys.stderr)
        
        # Match commands with outputs
        for i, cmd in enumerate(commands):
            cmd_str = cmd.strip()
            output = ""
            
            # 1. Exact match
            if cmd_str in echo_map:
                output = echo_map[cmd_str]
            else:
                # 2. Partial match: echo key contains command or vice versa
                for echo_cmd, echo_text in echo_map.items():
                    if cmd_str == echo_cmd or cmd_str in echo_cmd or echo_cmd in cmd_str:
                        output = echo_text
                        break
                # 3. Fallback: use first available output
                if not output and echo_map:
                    output = list(echo_map.values())[0]
            
            api_responses.append({
                "command": cmd_str,
                "output": output
            })
        
        return api_responses
        
    except Exception as e:
        print(f"# [WARNING] Failed to build api_responses: {e}", file=sys.stderr)
        return []


def build_response(step: Dict[str, Any], analysis_result: Dict[str, Any], response_data: str = "",
                    context_id: str = "", question_no: str = "", session_id: str = "",
                    commands_executed: int = 0, api_responses: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    step_num = step['step_number']
    step_name = step['step_name']
    status = analysis_result.get('status', 'unknown')
    message = analysis_result.get('message', '')
    next_step = analysis_result.get('next_step', '')

    answer_status = "SUCCESS" if status != "error" else "FAILED"

    emoji = "✅" if status == "normal" else ("❌" if status == "abnormal" else "⚠️")

    # 5.1 构造 stepCommand 消息：commands[].echo 已经带回显，前端
    #     检测到 echo 存在时会跳过对 deviceInfo 接口的调用，直接渲染
    step_command_commands = []
    if api_responses:
        for i, item in enumerate(api_responses):
            step_command_commands.append({
                "index": i,
                "command": item.get("command", ""),
                # echo 直接给字符串（前端 messageStore 已兼容 string 形式：
                # 见 messageStore handleMessage 中 `if typeof echo === "string"` 分支）
                "echo": item.get("output", "")
            })
    else:
        # 如果没有 api_responses，使用 step 中的 commands 和 response_data
        commands = step.get('commands', [])
        if commands and response_data:
            step_command_commands.append({
                "index": 0,
                "command": commands[0] if commands else "",
                "echo": response_data
            })

    step_command_msg = {
        "answerType": "stepCommand",
        "contextEnd": "false",
        "contextId": context_id,
        "currentStep": step_num,
        "message": {
            "commands": step_command_commands,
            "sessionId": session_id
        },
        "questionNo": question_no,
        "sessionId": session_id
    }

    # 5.2 构造 stepContent 分析结果消息
    step_content_msg = {
        "answerType": "stepContent",
        "contextEnd": "false",
        "contextId": context_id,
        "currentStep": step_num,
        "message": f"{emoji} {message}",
        "questionNo": question_no,
        "sessionId": session_id,
        "nextStep": next_step
    }

    # 5.3 输出 bundle：agent 端会按顺序逐条推送给前端
    bundle = {
        "stepBundle": [step_command_msg, step_content_msg],
        # 兼容字段：保留 stepContent 顶层字段，方便上层逻辑判断 answerType
        # 仍然按 stepContent 推进步骤
        "answerType": step_content_msg["answerType"],
        "contextEnd": step_content_msg["contextEnd"],
        "contextId": step_content_msg["contextId"],
        "currentStep": step_content_msg["currentStep"],
        "message": step_content_msg["message"],
        "questionNo": step_content_msg["questionNo"],
        "sessionId": step_content_msg["sessionId"],
        "nextStep": step_content_msg["nextStep"]
    }

    return bundle


def main():
    # Reconfigure stdout/stderr to use UTF-8 encoding to avoid Unicode issues on Windows
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass  # reconfigure not available or not needed
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
    if len(sys.argv) < 2:
        sys.stdout.buffer.write(json.dumps({"error": "Usage: python step_executor.py <mode> [params]"}).encode('utf-8'))
        sys.stdout.buffer.write(b'\n')
        sys.exit(1)

    mode = sys.argv[1]

    if mode == "build_and_execute":
        if len(sys.argv) < 3:
            sys.stdout.buffer.write(json.dumps({"error": "Missing parameters for build_and_execute mode"}).encode('utf-8'))
            sys.stdout.buffer.write(b'\n')
            sys.exit(1)

        try:
            params = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            sys.stdout.buffer.write(json.dumps({"error": f"Invalid JSON: {e}"}).encode('utf-8'))
            sys.stdout.buffer.write(b'\n')
            sys.exit(1)

        step_name = params.get("step_name", "")
        step_number = params.get("step_number", 1)
        device_info = params.get("device_info", {})
        session_id = params.get("session_id", "")
        skill_name = params.get("skill_name", "")
        history_data = params.get("history_data", "")
        context_id = params.get("context_id", "")
        question_no = params.get("question_no", "")
        commands_from_params = params.get("commands", [])

        sys.stderr.buffer.write(f"# [DEBUG] step_name: '{step_name}'\n".encode('utf-8'))
        sys.stderr.buffer.write(f"# [DEBUG] step_number: {step_number}\n".encode('utf-8'))

        # Extract destination_network parameter if present
        destination_network = params.get("destination_network", "")
        # If destination_network is empty or default, set it to a dummy value
        # to avoid field check (this skill doesn't need destination network)
        if not destination_network or destination_network == "0.0.0.0/0":
            destination_network = "192.168.1.0/24"
            params["destination_network"] = destination_network
        
        # Check if device IP is missing
        original_device_info = params.get("device_info", {})
        if isinstance(original_device_info, str):
            original_device_info = {"ip": original_device_info}
        original_device_ip = original_device_info.get("ip", "")
        original_device_ip_empty = not original_device_ip or original_device_ip.strip() == ""
        
        # If device IP is empty, request it from frontend
        if original_device_ip_empty:
            user_message = "需要补充以下信息以继续故障排查：<br/>"
            user_message += "- 设备IP地址：格式为 xxx.xxx.xxx.xxx（例如：192.168.1.1）<br/>"
            
            result = {
                "answerType": "conversation",
                "contextEnd": "false",
                "contextId": context_id,
                "currentStep": step_number,
                "message": user_message,
                "questionNo": question_no,
                "sessionId": session_id
            }
            sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('utf-8'))
            sys.exit(0)

        instructions = load_skill_instructions()
        steps = parse_skill_steps(instructions)
        sys.stderr.buffer.write(f"# [DEBUG] Total steps parsed: {len(steps)}\n".encode('utf-8'))
        
        current_step = None
        if step_name:
            for s in steps:
                sys.stderr.buffer.write(f"# [DEBUG] Comparing step: '{s['step_name']}' with search: '{step_name}'\n".encode('utf-8'))
                if step_name in s['step_name'] or s['step_name'] in step_name:
                    current_step = s
                    sys.stderr.buffer.write(f"# [DEBUG] Found step by name: {current_step['step_number']} - {current_step['step_name']}\n".encode('utf-8'))
                    break

        if current_step is None:
            for s in steps:
                if s['step_number'] == step_number:
                    current_step = s
                    sys.stderr.buffer.write(f"# [DEBUG] Found step by number: {current_step['step_number']} - {current_step['step_name']}\n".encode('utf-8'))
                    break

        if current_step is None:
            sys.stderr.buffer.write(f"# [WARNING] Step not found in SKILL.md: {step_name} (step {step_number})\n".encode('utf-8'))
            sys.stderr.buffer.write(f"# [WARNING] Creating default step from parameters\n".encode('utf-8'))
            # Create a default step from parameters
            current_step = {
                "step_number": step_number,
                "step_name": step_name,
                "title": step_name,
                "commands": commands_from_params,  # Use commands from parameters
                "rules": []
            }
            sys.stderr.buffer.write(f"# [DEBUG] Created default step: {current_step}\n".encode('utf-8'))

        # Priority: use commands from params if provided
        if "commands" in params:
            current_step["commands"] = commands_from_params
            sys.stderr.buffer.write(f"# [DEBUG] Using commands from params: {commands_from_params}\n".encode('utf-8'))
        else:
            sys.stderr.buffer.write(f"# [DEBUG] No commands in params, using from SKILL.md\n".encode('utf-8'))

        commands = current_step.get('commands', [])
        sys.stderr.buffer.write(f"# [DEBUG] Step commands: {commands}\n".encode('utf-8'))

        response_data = ""
        api_result = None
        api_responses = []
        
        if commands and device_info and device_info.get('ip'):
            api_result = execute_device_command(commands, device_info, session_id)
            response_data = extract_command_output(api_result)
            
            # Build api_responses for stepCommand
            # Handle multiple API response formats
            api_responses = build_api_responses_from_result(api_result, commands)
            
            # If api_responses is empty, fallback to using response_data
            if not api_responses and commands and response_data:
                api_responses.append({
                    "command": commands[0],
                    "output": response_data
                })
        elif history_data:
            response_data = history_data
            if commands and response_data:
                api_responses.append({
                    "command": commands[0] if commands else "",
                    "output": response_data
                })

        sys.stderr.buffer.write(f"# [DEBUG] Response data (first 500 chars): {response_data[:500]}\n".encode('utf-8'))

        analysis_result = analyze_step_result(current_step, response_data)
        sys.stderr.buffer.write(f"# [DEBUG] Analysis result: {json.dumps(analysis_result, ensure_ascii=True)}\n".encode('utf-8'))

        response = build_response(
            current_step, analysis_result, response_data,
            context_id=context_id, question_no=question_no, session_id=session_id,
            commands_executed=1 if response_data else 0,
            api_responses=api_responses
        )
        # Use sys.stdout.buffer to avoid encoding issues on Windows
        json_str = json.dumps(response, ensure_ascii=True)
        sys.stdout.buffer.write(json_str.encode('utf-8'))

    else:
        error_response = json.dumps({"error": f"Unknown mode: {mode}"}, ensure_ascii=True)
        sys.stdout.buffer.write(error_response.encode('utf-8'))
        sys.exit(1)


if __name__ == "__main__":
    main()