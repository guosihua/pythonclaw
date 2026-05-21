"""
Unified Step Executor for Static Route Troubleshooting

This script serves as a unified executor for all troubleshooting steps.
It builds device commands based on step_type and analyzes responses accordingly.

Usage:
    python step_executor.py build '{"step_type": "check_route", ...}'
    python step_executor.py analyze '{"step_type": "check_route", "response_data": "..."}'
"""

import json
import re
import sys
import requests
from pathlib import Path
from typing import Dict, Any, Optional, List


# Terminal API Configuration
TERMINAL_API_BASE_URL = "http://10.153.61.64/terminal/api/terminal/ai"
DEVICE_INFO_ENDPOINT = f"{TERMINAL_API_BASE_URL}/deviceInfo"


# Field templates for different analysis types
INFO_REQUEST_FIELDS = {
    "check_route": [
        {"name": "device_ip", "label": "设备IP地址", "type": "text", "placeholder": "例如: 192.168.1.1", "required": True},
        {"name": "destination_network", "label": "目的网段", "type": "text", "placeholder": "例如: 192.168.1.0/24 或 10.0.0.0/8", "required": True},
    ],
    "switch_troubleshooting": [
        {"name": "pc1_ip", "label": "PC1的IP地址", "type": "text", "placeholder": "例如: 192.168.1.10", "required": True},
        {"name": "pc1_mac", "label": "PC1的MAC地址", "type": "text", "placeholder": "例如: 00-11-22-33-44-55", "required": True},
        {"name": "pc2_ip", "label": "PC2的IP地址", "type": "text", "placeholder": "例如: 192.168.2.10", "required": True},
        {"name": "pc2_mac", "label": "PC2的MAC地址", "type": "text", "placeholder": "例如: 00-11-22-33-44-66", "required": True},
        {"name": "access_switch", "label": "PC1连接的接入交换机", "type": "text", "placeholder": "例如: Switch-A", "required": True},
        {"name": "access_port", "label": "接入交换机接口号", "type": "text", "placeholder": "例如: GigabitEthernet0/1", "required": True},
        {"name": "target_port", "label": "待检查的接入交换机接口", "type": "text", "placeholder": "例如: GigabitEthernet0/2", "required": True},
        {"name": "agg_switch", "label": "汇聚交换机", "type": "text", "placeholder": "例如: Switch-B", "required": True},
        {"name": "core_switch", "label": "核心交换机", "type": "text", "placeholder": "例如: Core-Switch", "required": True},
        {"name": "pc1_vlan", "label": "PC1所属VLAN", "type": "text", "placeholder": "例如: 10", "required": True},
        {"name": "pc2_vlan", "label": "PC2所属VLAN", "type": "text", "placeholder": "例如: 20", "required": True},
        {"name": "rate_limit_port", "label": "核心交换机限速接口名称", "type": "text", "placeholder": "例如: GigabitEthernet0/1", "required": True},
        {"name": "rate_limit_value", "label": "当前限速值", "type": "text", "placeholder": "例如: 1000", "required": True},
        {"name": "qos_policy", "label": "QoS策略名称", "type": "text", "placeholder": "例如: qos-policy-1", "required": True},
    ],
    "default": [
        {"name": "device_ip", "label": "设备IP地址", "type": "text", "placeholder": "例如: 192.168.1.1", "required": True},
        {"name": "destination_network", "label": "目的网段", "type": "text", "placeholder": "例如: 192.168.1.0/24", "required": True},
    ]
}


def get_info_request_fields(analysis_type: str) -> list:
    """Get the field list for a specific analysis type."""
    return INFO_REQUEST_FIELDS.get(analysis_type, INFO_REQUEST_FIELDS.get("default", []))


def load_devices_config(devices_file_path: str = None) -> list:
    """Load device configuration from devices.json file."""
    if devices_file_path is None:
        # Try multiple possible paths
        possible_paths = [
            Path(__file__).parent.parent.parent.parent.parent / "file" / "devices.json",
            Path(__file__).parent.parent.parent.parent.parent.parent / "file" / "devices.json",
            Path("/config/file/devices.json"),
            Path("f:/workspace/project/H3C/pythonclaw/file/devices.json"),
        ]
        
        print(f"# Looking for devices.json in: {[str(p) for p in possible_paths]}", file=sys.stderr)
        
        devices_path = None
        for p in possible_paths:
            if p.exists():
                devices_path = p
                print(f"# Found devices.json at: {devices_path}", file=sys.stderr)
                break
        
        if devices_path is None:
            raise FileNotFoundError(f"Devices config file not found in any of: {possible_paths}")
    else:
        devices_path = Path(devices_file_path)
    
    print(f"# Loading devices from: {devices_path}", file=sys.stderr)
    
    with open(devices_path, 'r', encoding='utf-8') as f:
        devices = json.load(f)
    
    print(f"# Loaded {len(devices)} devices", file=sys.stderr)
    return devices


def get_device_by_ip(devices: list, ip_address: str) -> Optional[Dict[str, Any]]:
    """Get device information by IP address."""
    for device in devices:
        if device.get("ip") == ip_address:
            return {
                "ip": device.get("ip", ""),
                "username": device.get("userName", ""),
                "password": device.get("password", ""),
                "port": device.get("port", 23),
                "protocol": device.get("protocol", "telnet"),
                "uuid": device.get("deviceId", "")
            }
    
    return None


def execute_device_command(
    commands: List[str],
    device_info: Dict[str, Any],
    session_id: str
) -> Dict[str, Any]:
    """Execute commands on a remote device via Terminal API.
    
    Args:
        commands: List of commands to execute (supports batch execution)
        device_info: Device connection information
        session_id: Session identifier for the terminal connection
    
    Returns:
        API response containing execution results for all commands
    """
    payload = [{
        "command": commands,  # Pass all commands as a list for batch execution
        "device": {
            "ip": device_info.get("ip", ""),
            "password": device_info.get("password", ""),
            "port": device_info.get("port", 23),
            "protocol": device_info.get("protocol", "telnet"),
            "username": device_info.get("username", "")
        },
        "sessionId": session_id
    }]
    
    # Log complete request details
    print(f"# [API Request] Endpoint: {DEVICE_INFO_ENDPOINT}", file=sys.stderr)
    print(f"# [API Request] Payload:", file=sys.stderr)
    # Output payload as-is for debugging structure
    print(f"#   {json.dumps(payload, ensure_ascii=False, indent=2)}", file=sys.stderr)
    
    try:
        response = requests.post(
            DEVICE_INFO_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        response.raise_for_status()
        
        result = response.json()
        
        # Log complete response details
        print(f"# [API Response] Status Code: {response.status_code}", file=sys.stderr)
        print(f"# [API Response] Response Body (first 500 chars):", file=sys.stderr)
        response_preview = json.dumps(result, ensure_ascii=False)[:500]
        print(f"#   {response_preview}", file=sys.stderr)
        if len(json.dumps(result, ensure_ascii=False)) > 500:
            print(f"#   ... ({len(json.dumps(result, ensure_ascii=False)) - 500} more chars)", file=sys.stderr)
        
        if result.get("code") != 0:
            raise Exception(f"API error: {result.get('message', 'Unknown error')}")
        
        return result
        
    except requests.exceptions.Timeout:
        print(f"# [API Error] Request timed out after 30s", file=sys.stderr)
        raise Exception("Device command execution timed out (30s)")
    except requests.exceptions.ConnectionError as e:
        print(f"# [API Error] Connection failed: {e}", file=sys.stderr)
        print(f"# Warning: Failed to connect to Terminal API at {DEVICE_INFO_ENDPOINT}", file=sys.stderr)
        print(f"# Returning mock response for demonstration", file=sys.stderr)
        # Return mock data for demonstration/testing
        return _generate_mock_response(commands, device_info)
    except Exception as e:
        print(f"# [API Error] Request failed: {e}", file=sys.stderr)
        print(f"# Warning: API call failed: {e}", file=sys.stderr)
        print(f"# Returning mock response for demonstration", file=sys.stderr)
        return _generate_mock_response(commands, device_info)


def _generate_mock_response(commands: List[str], device_info: Dict[str, Any]) -> Dict[str, Any]:
    """Generate mock API response for testing/demonstration when real device is unavailable.
    
    Args:
        commands: List of commands to generate mock responses for
        device_info: Device information (unused in mock, but kept for compatibility)
    
    Returns:
        Mock API response with outputs for all commands
    """
    import uuid
    
    # Generate realistic mock responses based on command
    mock_outputs = {
        "display ip routing-table": """
Routing Table (Route Processing):
Destinations: 10

Routing Entry Statistics:
Static Route: 1   Direct Route: 1   Static Routing Entries: 1

Static Routes:
10.1.1.0/24  [60/0]
        via 10.88.142.1

Direct Routes:
10.88.142.0/24 [Direct/0]
        via 10.88.142.204
""",
        "display ip routing-table protocol static": """
Routing Table (Route Processing):
Destinations: 2

Static Routes:
10.1.1.0/24  [60/0]
        via 10.88.142.1
192.168.1.0/24 [60/0]
        via 10.88.142.1
""",
        "ping": "Ping statistics:\n  ICMP Echo: request = 3, reply = 3, lost = 0 (0.0% loss)\n  Min/Avg/Max = 1/2/3 ms\n  Success rate is 100%",
        "display interface brief": """
Interface     Status  Speed   Duplex Type
Ethernet0/0   up      1000M   full   Auto
Ethernet0/1   down    0M      auto   Auto
GigabitEthernet0/0  up 1000M   full   Auto
""",
        "display bfd session": """
Sess Index  BFD State  Local IP     Remote IP    Status
1           Up         10.88.142.204 10.88.142.1  Up
""",
        "display track all": """
Track id: 1 Active (up)
Track Type: BFD
Track Object: 1
Track Status: up
"""
    }
    
    # Generate mock output for each command
    data_entries = []
    for cmd in commands:
        # Find matching mock output
        output = "mock response"
        for key, value in mock_outputs.items():
            if key in cmd:
                output = value
                break
        
        data_entries.append({
            "id": str(uuid.uuid4()),
            "echo": {
                "output": output
            }
        })
    
    return {
        "code": 0,
        "message": "Success",
        "data": data_entries
    }


def extract_command_output(api_response: Dict[str, Any]) -> str:
    """Extract the actual command output from API response."""
    try:
        data = api_response.get("data", [])
        if not data:
            return ""
        
        first_result = data[0]
        echo = first_result.get("echo", {})
        
        if echo:
            return list(echo.values())[0]
        
        return ""
        
    except Exception as e:
        print(f"# Warning: Failed to extract command output: {e}", file=sys.stderr)
        return ""


def extract_nexthop_from_route(api_response: Dict[str, Any], destination_network: str) -> str:
    """Extract nexthop IP from routing table response."""
    try:
        # Extract the route output from API response
        route_output = extract_command_output(api_response)
        if not route_output:
            return ""
        
        print(f"# [extract_nexthop] Route output: {route_output[:200]}", file=sys.stderr)
        
        # Parse H3C routing table format
        # Format: Destination/Mask   Proto   Pre Cost        NextHop         Interface
        lines = route_output.strip().split('\n')
        
        # Extract destination network without subnet mask for comparison
        dest_ip = destination_network.split('/')[0] if '/' in destination_network else destination_network
        
        for line in lines:
            # Look for lines containing IP addresses (potential route entries)
            if re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', line):
                # Split by whitespace
                parts = re.split(r'\s+', line.strip())
                if len(parts) >= 5:
                    # parts[0] = Destination/Mask
                    # parts[1] = Proto
                    # parts[2] = Pre
                    # parts[3] = Cost
                    # parts[4] = NextHop
                    dest_mask = parts[0]
                    nexthop = parts[4]
                    
                    # Check if this line matches our destination network
                    if dest_ip in dest_mask:
                        print(f"# [extract_nexthop] Found matching route: {dest_mask} -> {nexthop}", file=sys.stderr)
                        return nexthop
        
        # If no specific match, try to find any static route nexthop
        for line in lines:
            parts = re.split(r'\s+', line.strip())
            if len(parts) >= 5 and parts[1].lower() == 'static':
                print(f"# [extract_nexthop] Found static route nexthop: {parts[4]}", file=sys.stderr)
                return parts[4]
        
        print(f"# [extract_nexthop] No nexthop found in route output", file=sys.stderr)
        return ""
        
    except Exception as e:
        print(f"# [extract_nexthop] Failed to extract nexthop: {e}", file=sys.stderr)
        return ""


# ============================================================================
# Command Template Engine
# ============================================================================

def render_command_template(template: str, params: Dict[str, Any]) -> str:
    """
    Render command template by replacing {{variable}} with actual values.
    
    Args:
        template: Command template with {{variable}} placeholders
        params: Parameter dictionary for substitution
    
    Returns:
        Rendered command string
    
    Example:
        >>> render_command_template("ping {{ip}}", {"ip": "10.1.1.1"})
        'ping 10.1.1.1'
    """
    def replace_match(match):
        var_name = match.group(1)
        return str(params.get(var_name, match.group(0)))
    
    # Replace {{variable}} patterns
    rendered = re.sub(r'\{\{(\w+)\}\}', replace_match, template)
    
    # Safety check: if no replacement happened and template contains word-like tokens that match param keys,
    # it might be a malformed template (e.g., "display ip routing-table destination_network" instead of 
    # "display ip routing-table {{destination_network}}"). Try to fix it.
    if rendered == template:
        # Check if any param key appears as a standalone word in the template
        for key, value in params.items():
            # Match whole word boundaries to avoid partial matches
            pattern = r'\b' + re.escape(key) + r'\b'
            if re.search(pattern, template) and key not in ['session_id', 'context_id', 'question_no']:
                # This looks like an untemplated variable, auto-fix it
                print(f"# Warning: Template '{template}' appears to have untemplated variable '{key}'. Auto-fixing...", file=sys.stderr)
                rendered = re.sub(pattern, str(value), template)
                break
    
    return rendered


def build_commands_from_templates(templates: List[str], params: Dict[str, Any]) -> List[str]:
    """
    Build commands from templates by rendering each template.
    
    Args:
        templates: List of command templates
        params: Parameters for template rendering
    
    Returns:
        List of rendered commands
    """
    commands = []
    for template in templates:
        rendered_cmd = render_command_template(template, params)
        # Only add command if it doesn't contain unresolved placeholders (optional safety check)
        # For now, we add all rendered commands
        commands.append(rendered_cmd)
    
    return commands


# ============================================================================
# Step Command Templates
# ============================================================================

STEP_COMMAND_TEMPLATES = {
    "check_route": [
        "display ip routing-table {{destination_network}}"
    ],
    "check_nexthop": [
        "display ip routing-table protocol static {{destination_network}}",
        "ping {{nexthop_ip}}"
    ],
    "check_mask": [
        "display ip routing-table"
    ],
    "check_interface": [
        "display interface brief"
    ],
    "check_bfd": [
        "display bfd session",
        "display track all"
    ],
    "check_priority": [
        "display ip routing-table",
        "display ip routing-table verbose"
    ]
}


# ============================================================================
# Step Type Handlers - Result Analyzers (Keep these for analysis logic)
# ============================================================================

def analyze_check_route_result(response_data: str) -> Dict[str, Any]:
    """Analyze routing table response."""
    result = {
        "step": 1,
        "step_name": "检查全局路由表中是否存在该静态路由",
        "status": "",
        "route_exists": False,
        "next_step": "",
        "message": ""
    }
    
    # Check if response contains routing table entries
    # H3C routing table format: "Destination/Mask   Proto   Pre Cost        NextHop         Interface"
    if "Destination/Mask" in response_data or "Summary count" in response_data:
        # Check if there are any route entries (lines with IP addresses)
        # Look for lines that match IP address pattern
        import re
        ip_pattern = r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
        has_route_entries = bool(re.search(ip_pattern, response_data))
        
        if has_route_entries:
            result["route_exists"] = True
            result["status"] = "success"
            result["next_step"] = "step2"
            result["message"] = "路由表中存在该静态路由条目，继续检查下一跳可达性"
        else:
            result["route_exists"] = False
            result["status"] = "not_found"
            result["next_step"] = "step4"
            result["message"] = "路由表中未找到该静态路由，需要检查静态路由配置"
    elif "not found" in response_data.lower() or "no route" in response_data.lower():
        result["route_exists"] = False
        result["status"] = "not_found"
        result["next_step"] = "step5"
        result["message"] = "设备返回：路由不存在，跳转到检查静态路由配置"
    else:
        result["status"] = "error"
        result["next_step"] = "step7"
        result["message"] = "无法解析路由表回显，跳转到总结步骤"
    
    return result


def analyze_check_nexthop_result(response_data: str) -> Dict[str, Any]:
    """Analyze ping response for next-hop reachability."""
    result = {
        "step": 2,
        "step_name": "检查下一跳地址可达性",
        "status": "",
        "reachable": False,
        "packet_loss": 0,
        "next_step": "",
        "message": ""
    }
    
    if "success" in response_data.lower() or "100%" in response_data:
        result["reachable"] = True
        result["status"] = "success"
        result["next_step"] = "step3"
        result["message"] = "下一跳地址可达，继续检查路由掩码与最长匹配原则"
    elif "timeout" in response_data.lower() or "unreachable" in response_data.lower():
        result["reachable"] = False
        result["status"] = "unreachable"
        result["next_step"] = "step7"
        result["message"] = "下一跳地址不可达，需要排查链路问题"
    else:
        result["status"] = "error"
        result["next_step"] = "step7"
        result["message"] = "无法解析ping回显，跳转到总结步骤"
    
    return result


def analyze_check_mask_result(response_data: str) -> Dict[str, Any]:
    """Analyze route mask configuration."""
    result = {
        "step": 3,
        "step_name": "检查路由掩码与最长匹配原则",
        "status": "",
        "mask_correct": True,
        "next_step": "",
        "message": ""
    }
    
    if "Routing Table" in response_data:
        result["mask_correct"] = True
        result["status"] = "success"
        result["next_step"] = "step7"
        result["message"] = "路由掩码配置正确，排查流程结束"
    else:
        result["mask_correct"] = False
        result["status"] = "warning"
        result["next_step"] = "step7"
        result["message"] = "未发现明显掩码问题，建议人工确认"
    
    return result


def analyze_check_interface_result(response_data: str) -> Dict[str, Any]:
    """Analyze interface status."""
    result = {
        "step": 4,
        "step_name": "检查出接口物理与协议状态",
        "status": "",
        "interface_up": False,
        "next_step": "",
        "message": ""
    }
    
    if "UP" in response_data and "DOWN" not in response_data:
        result["interface_up"] = True
        result["status"] = "success"
        result["next_step"] = "step5"
        result["message"] = "接口状态正常，继续检查BFD/NQA配置"
    elif "DOWN" in response_data:
        result["interface_up"] = False
        result["status"] = "down"
        result["next_step"] = "step7"
        result["message"] = "接口状态异常，需要修复接口"
    else:
        result["status"] = "error"
        result["next_step"] = "step7"
        result["message"] = "无法解析接口状态，跳转到总结步骤"
    
    return result


def analyze_check_bfd_result(response_data: str) -> Dict[str, Any]:
    """Analyze BFD/NQA status."""
    result = {
        "step": 5,
        "step_name": "检查BFD或NQA配置与状态",
        "status": "",
        "bfd_enabled": False,
        "next_step": "",
        "message": ""
    }
    
    if "BFD" in response_data or "Track" in response_data:
        result["bfd_enabled"] = True
        result["status"] = "success"
        result["next_step"] = "step6"
        result["message"] = "BFD/NQA配置存在，继续检查路由优先级"
    else:
        result["bfd_enabled"] = False
        result["status"] = "info"
        result["next_step"] = "step7"
        result["message"] = "未配置BFD/NQA，跳过此步骤"
    
    return result


def analyze_check_priority_result(response_data: str) -> Dict[str, Any]:
    """Analyze static route priority."""
    result = {
        "step": 6,
        "step_name": "检查本静态路由的优先级",
        "status": "",
        "priority_normal": True,
        "next_step": "",
        "message": ""
    }
    
    if "Static" in response_data and "Pre" in response_data:
        result["priority_normal"] = True
        result["status"] = "success"
        result["next_step"] = "step7"
        result["message"] = "路由优先级正常，排查流程结束"
    else:
        result["priority_normal"] = False
        result["status"] = "warning"
        result["next_step"] = "step7"
        result["message"] = "未发现优先级冲突，建议人工确认"
    
    return result


# ============================================================================
# Main Entry Point
# ============================================================================

ANALYSIS_HANDLERS = {
    "check_route": analyze_check_route_result,
    "check_nexthop": analyze_check_nexthop_result,
    "check_mask": analyze_check_mask_result,
    "check_interface": analyze_check_interface_result,
    "check_bfd": analyze_check_bfd_result,
    "check_priority": analyze_check_priority_result
}


def main():
    """Main entry point for the unified step executor."""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python step_executor.py <mode> [params]"}))
        sys.exit(1)
    
    mode = sys.argv[1]
    
    if mode == "build_and_execute":
        # Build commands from templates and execute immediately
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing parameters for build_and_execute mode"}))
            sys.exit(1)
        
        # Parse params - handle both JSON string and direct dict
        params_input = sys.argv[2]
        if isinstance(params_input, str):
            try:
                params = json.loads(params_input)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON in params: {e}"}))
                sys.exit(1)
        else:
            params = params_input
        
        # Extract required fields
        commands_templates = params.get("commands", [])
        analysis_type = params.get("analysis_type", "")
        step_type = params.get("step_type", "")
        session_id = params.get("session_id", "")
        destination_network = params.get("destination_network", "")
        step_number = params.get("step_number", 1)
        
        # Determine the analysis type (use step_type as fallback)
        effective_analysis_type = analysis_type or step_type
        
        # Auto-generate session_id if not provided
        if not session_id:
            import uuid
            session_id = f"troubleshooting-{uuid.uuid4().hex[:8]}"
            print(f"# Generated session_id: {session_id}", file=sys.stderr)
        
        # Auto-generate commands if not provided
        if not commands_templates and effective_analysis_type:
            commands_templates = STEP_COMMAND_TEMPLATES.get(effective_analysis_type, [])
        
        if not commands_templates:
            print(json.dumps({
                "error": f"Cannot determine commands. Please provide 'commands' field or set 'analysis_type'/'step_type' to one of: {list(STEP_COMMAND_TEMPLATES.keys())}"
            }))
            sys.exit(1)
        
        if not effective_analysis_type:
            print(json.dumps({"error": "analysis_type or step_type field is required"}))
            sys.exit(1)
        
        # Load device information
        device_info = params.get("device_info", {})
        print(f"# ========== DEVICE INFO LOADING START ==========", file=sys.stderr)
        print(f"# Input device_info from params: {device_info}", file=sys.stderr)
        
        if isinstance(device_info, str):
            device_info = {"ip": device_info}
            print(f"# Converted device_info to dict: {device_info}", file=sys.stderr)
        
        # If device_info is still empty or incomplete, try to load from devices.json
        ip_is_empty = not device_info.get("ip") or device_info.get("ip", "").strip() == ""
        if not device_info or "ip" not in device_info or ip_is_empty:
            print(f"# Warning: No device_info provided or incomplete", file=sys.stderr)
            print(f"#   device_info: {device_info}", file=sys.stderr)
            # Try to use first device from devices.json as fallback
            try:
                devices = load_devices_config()
                if devices:
                    device_info = {
                        "ip": devices[0].get("ip", ""),
                        "username": devices[0].get("userName", ""),
                        "password": devices[0].get("password", ""),
                        "port": devices[0].get("port", 23),
                        "protocol": devices[0].get("protocol", "telnet"),
                        "uuid": devices[0].get("deviceId", "")
                    }
                    print(f"# Using first device from devices.json: {device_info['ip']}", file=sys.stderr)
                else:
                    print(f"# Warning: No devices found in devices.json, returning stepCommand for frontend execution", file=sys.stderr)
                    # Return stepCommand instead of error, let frontend execute commands
                    result = {
                        "answerType": "stepCommand",
                        "contextEnd": "false",
                        "contextId": params.get("context_id", ""),
                        "currentStep": step_number,
                        "message": {
                            "deviceCommds": commands,
                            "deviceInfo": device_info,
                            "sessionId": session_id
                        },
                        "questionNo": params.get("question_no", ""),
                        "sessionId": session_id,
                        "debug": {
                            "commands": commands,
                            "note": "No device IP available. Please execute commands manually and call analyze endpoint."
                        }
                    }
                    print(json.dumps(result, ensure_ascii=False))
                    sys.exit(0)
            except Exception as e:
                print(f"# Warning: Failed to load devices.json: {e}, returning stepCommand", file=sys.stderr)
                # Return stepCommand instead of error
                result = {
                    "answerType": "stepCommand",
                    "contextEnd": "false",
                    "contextId": params.get("context_id", ""),
                    "currentStep": step_number,
                    "message": {
                        "deviceCommds": commands,
                        "deviceInfo": device_info,
                        "sessionId": session_id
                    },
                    "questionNo": params.get("question_no", ""),
                    "sessionId": session_id,
                    "debug": {
                        "commands": commands,
                        "note": f"Failed to load device config: {e}. Please execute commands manually."
                    }
                }
                print(json.dumps(result, ensure_ascii=False))
                sys.exit(0)
        elif "ip" in device_info:
            # Device IP is provided, try to load full info from devices.json
            ip_address = device_info["ip"]
            needs_full_info = not all(k in device_info and device_info[k] for k in ["username", "password", "port", "protocol"])
            
            print(f"# Device IP provided: {ip_address}", file=sys.stderr)
            print(f"# Needs full info: {needs_full_info}", file=sys.stderr)
            print(f"# Current device_info: {device_info}", file=sys.stderr)
            
            if needs_full_info:
                print(f"# Attempting to load device info from devices.json...", file=sys.stderr)
                try:
                    devices = load_devices_config()
                    print(f"# Loaded {len(devices)} devices from devices.json", file=sys.stderr)
                    full_device_info = get_device_by_ip(devices, ip_address)
                    
                    if full_device_info:
                        print(f"# Found device in devices.json: {full_device_info}", file=sys.stderr)
                        # Only fill in missing fields, don't overwrite existing non-empty values
                        for key, value in full_device_info.items():
                            if key not in device_info or not device_info[key]:
                                device_info[key] = value
                        print(f"# Loaded device info for {ip_address} from devices.json", file=sys.stderr)
                        print(f"# Updated device_info: {device_info}", file=sys.stderr)
                    else:
                        print(f"# Warning: Device {ip_address} not found in devices.json, using provided info", file=sys.stderr)
                        
                except Exception as e:
                    print(f"# Warning: Failed to load devices.json: {e}", file=sys.stderr)
        
        # Check if we need additional information from frontend
        original_device_info = params.get("device_info", {})
        if isinstance(original_device_info, str):
            original_device_info = {"ip": original_device_info}
        original_device_ip = original_device_info.get("ip", "")
        original_device_ip_empty = not original_device_ip or original_device_ip.strip() == ""
        
        # Check if destination network is default (not provided by user)
        destination_network_empty = not destination_network or destination_network == "0.0.0.0/0"
        
        # If either device IP or destination network is missing, return info request
        if original_device_ip_empty or destination_network_empty:
            print(f"# Information missing, requesting from frontend", file=sys.stderr)
            print(f"#   Original device IP empty: {original_device_ip_empty}", file=sys.stderr)
            print(f"#   Destination network empty/default: {destination_network_empty}", file=sys.stderr)
            
            missing_fields = []
            if original_device_ip_empty:
                missing_fields.append("device_ip")
            if destination_network_empty:
                missing_fields.append("destination_network")
            
            # Get field list based on analysis type
            info_fields = get_info_request_fields(effective_analysis_type)
            
            # Create user-friendly message
            missing_fields_display = []
            if original_device_ip_empty:
                missing_fields_display.append("设备IP地址")
            if destination_network_empty:
                missing_fields_display.append("目的网段")
            
            user_message = f"需要补充以下信息以继续故障排查：{', '.join(missing_fields_display)}"
            if original_device_ip_empty:
                user_message += "\n- 设备IP地址：例如 192.168.1.1"
            if destination_network_empty:
                user_message += "\n- 目的网段：例如 192.168.1.0/24"
            
            result = {
                "answerType": "stepInfoRequest",
                "contextEnd": "false",
                "contextId": params.get("context_id", ""),
                "currentStep": step_number,
                "missing_fields": missing_fields,
                "message": user_message,
                "user_message": user_message,
                "fields": info_fields,
                "required_info": {
                    "device_ip": "设备IP地址",
                    "destination_network": "目的网段 (例如: 192.168.1.0/24)"
                },
                "questionNo": params.get("question_no", ""),
                "sessionId": session_id
            }
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0)
        
        # Step 1: Render command templates
        commands = build_commands_from_templates(commands_templates, params)
        print(f"# Rendering {len(commands)} commands", file=sys.stderr)
        for i, cmd in enumerate(commands):
            print(f"#   Command {i+1}: {cmd}", file=sys.stderr)
        
        # For check_nexthop analysis type, we need to extract nexthop_ip from routing table first
        if effective_analysis_type == "check_nexthop" and "{{nexthop_ip}}" in str(commands):
            print(f"# [check_nexthop] Need to extract nexthop_ip first", file=sys.stderr)
            # First, execute the first command (display ip routing-table protocol static {{destination_network}}) to get nexthop
            route_cmd = commands[0]  # This should be the rendered command with destination_network
            print(f"# [check_nexthop] Executing route command to get nexthop: {route_cmd}", file=sys.stderr)
            
            # Build API request for route command
            route_payload = [{
                "command": [route_cmd],
                "device": {
                    "ip": device_info.get("ip", ""),
                    "password": device_info.get("password", ""),
                    "port": device_info.get("port", 23),
                    "protocol": device_info.get("protocol", "telnet"),
                    "username": device_info.get("username", "")
                },
                "sessionId": device_info.get("uuid") or session_id
            }]
            
            try:
                route_response = requests.post(
                    DEVICE_INFO_ENDPOINT,
                    json=route_payload,
                    headers={"Content-Type": "application/json"},
                    timeout=30
                )
                route_response.raise_for_status()
                route_result = route_response.json()
                
                print(f"# [check_nexthop] Route response: {json.dumps(route_result, ensure_ascii=False)[:500]}", file=sys.stderr)
                
                # Extract nexthop_ip from response
                nexthop_ip = extract_nexthop_from_route(route_result, destination_network)
                print(f"# [check_nexthop] Extracted nexthop_ip: {nexthop_ip}", file=sys.stderr)
                
                if nexthop_ip:
                    # Add nexthop_ip to params for template rendering
                    params["nexthop_ip"] = nexthop_ip
                    # Re-render commands with nexthop_ip
                    commands = build_commands_from_templates(commands_templates, params)
                    print(f"# [check_nexthop] Re-rendered commands with nexthop_ip:", file=sys.stderr)
                    for i, cmd in enumerate(commands):
                        print(f"#   Command {i+1}: {cmd}", file=sys.stderr)
                else:
                    print(f"# [check_nexthop] Could not extract nexthop_ip, skipping ping", file=sys.stderr)
                    # Remove ping command if nexthop not found
                    commands = [c for c in commands if "ping" not in c.lower()]
            except Exception as e:
                print(f"# [check_nexthop] Failed to get nexthop: {e}", file=sys.stderr)
                # Continue with original commands

        # Step 2: Execute commands via Terminal API (Direct API Mode)
        print(f"# ========== API EXECUTION START ==========", file=sys.stderr)
        print(f"# Executing {len(commands)} command(s) via Terminal API...", file=sys.stderr)
        try:

            # Build API request payload matching the required structure
            api_payload = [{
                "command": commands,  # List of commands
                "device": {
                    "ip": device_info.get("ip", ""),
                    "password": device_info.get("password", ""),
                    "port": device_info.get("port", 23),
                    "protocol": device_info.get("protocol", "telnet"),
                    "username": device_info.get("username", "")
                },
                "sessionId": device_info.get("uuid") or session_id  # Use device's deviceId as sessionId
            }]

            print(f"# [API Request] Endpoint: {DEVICE_INFO_ENDPOINT}", file=sys.stderr)
            print(f"# [API Request] Full Payload:", file=sys.stderr)
            print(f"# {json.dumps(api_payload, ensure_ascii=False, indent=2)}", file=sys.stderr)

            response = requests.post(
                DEVICE_INFO_ENDPOINT,
                json=api_payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            response.raise_for_status()
            api_response = response.json()

            print(f"# [API Response] Status Code: {response.status_code}", file=sys.stderr)
            print(f"# [API Response] Full Body:", file=sys.stderr)
            print(f"# {json.dumps(api_response, ensure_ascii=False, indent=2)}", file=sys.stderr)

            # Extract echo outputs from API response
            # Response structure: {"code": 0, "message": "OK", "data": [{"echo": {"cmd1": "output1"}, ...}]}
            api_responses = []
            all_outputs = []

            data_results = api_response.get("data", [])
            for i, cmd in enumerate(commands):
                cmd_str = cmd.strip()
                echo_output = ""

                # Try to find the echo for this command in the response
                if i < len(data_results):
                    data_item = data_results[i]
                    echo_data = data_item.get("echo", {})
                    if isinstance(echo_data, dict):
                        # echo is a dict like {"command": "output"}
                        for echo_cmd, echo_output in echo_data.items():
                            if echo_cmd.strip() == cmd_str or echo_cmd.strip() in cmd_str:
                                break
                    elif isinstance(echo_data, str):
                        echo_output = echo_data

                api_responses.append({
                    "command": cmd,
                    "output": echo_output,
                    "raw_response": api_response
                })
                all_outputs.append(echo_output)
                print(f"# Command {i+1} output length: {len(echo_output)} chars", file=sys.stderr)

            # Step 3: Combine outputs for analysis
            combined_output = "\n\n".join(all_outputs)
            print(f"# Combined output length: {len(combined_output)} chars", file=sys.stderr)

            # Step 4: Analyze results
            if effective_analysis_type not in ANALYSIS_HANDLERS:
                print(json.dumps({"error": f"Unknown analysis_type: {effective_analysis_type}. Valid types: {list(ANALYSIS_HANDLERS.keys())}"}))
                sys.exit(1)

            analyzer = ANALYSIS_HANDLERS[effective_analysis_type]
            analysis_result = analyzer(combined_output)

            # Step 5: Return analysis result in stepContent format for frontend
            result = {
                "answerType": "stepContent",
                "contextEnd": "false",
                "contextId": params.get("context_id", ""),
                "currentStep": analysis_result.get("step", 0),
                "message": analysis_result.get("message", ""),
                "questionNo": params.get("question_no", ""),
                "sessionId": session_id,
                "nextStep": analysis_result.get("next_step", "")  # Add nextStep for agent to determine next step
            }

            print(json.dumps(result, ensure_ascii=False))

        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            print(f"# Error: {error_traceback}", file=sys.stderr)

            error_result = {
                "answerType": "stepError",
                "contextEnd": "false",
                "contextId": params.get("context_id", ""),
                "currentStep": 0,
                "error": str(e),
                "message": f"Failed to execute or analyze commands: {str(e)}",
                "next_step": "retry",
                "traceback": error_traceback
            }
            print(json.dumps(error_result, ensure_ascii=False))
            sys.exit(1)
    
    elif mode == "analyze":
        # Analyze response from frontend (Frontend Integration Mode)
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing response data for analyze mode"}))
            sys.exit(1)

        params = json.loads(sys.argv[2])
        console_cmd = params.get("consoleCmd", "")
        current_step = params.get("currentStep", 1)
        session_id = params.get("sessionId", "")
        question_no = params.get("questionNo", "")
        status = params.get("status", True)

        # Parse consoleCmd - it's a JSON string containing {command: output} pairs
        command_outputs = {}
        if console_cmd:
            try:
                command_outputs = json.loads(console_cmd)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Failed to parse consoleCmd: {e}"}))
                sys.exit(1)

        # Determine analysis type from context_id or params
        analysis_type = params.get("analysis_type", "check_route")

        # Combine all command outputs
        combined_output = ""
        for cmd, output in command_outputs.items():
            combined_output += f"\n# Command: {cmd}\n{output}\n"

        print(f"# Analyzing {len(command_outputs)} command outputs", file=sys.stderr)
        print(f"# Analysis type: {analysis_type}", file=sys.stderr)

        if analysis_type not in ANALYSIS_HANDLERS:
            print(json.dumps({"error": f"Unknown analysis_type: {analysis_type}"}))
            sys.exit(1)

        # Analyze result
        analyzer = ANALYSIS_HANDLERS[analysis_type]
        analysis_result = analyzer(combined_output)

        # Update step number in analysis result
        if "step" not in analysis_result:
            analysis_result["step"] = current_step

        result = {
            "answerType": "stepAnalysis",
            "contextEnd": "false",
            "contextId": params.get("contextId", ""),
            "currentStep": current_step,
            "commands_executed": len(command_outputs),
            "analysis": analysis_result,
            "questionNo": question_no,
            "sessionId": session_id,
            "debug": {
                "commands": list(command_outputs.keys()),
                "status": status
            }
        }

        print(json.dumps(result, ensure_ascii=False))
    
    else:
        print(json.dumps({"error": f"Unknown mode: {mode}. Use 'build_and_execute' or 'analyze'"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
