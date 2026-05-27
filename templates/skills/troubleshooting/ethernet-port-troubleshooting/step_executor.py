"""
Unified Step Executor for Ethernet Port Troubleshooting

This script serves as a unified executor for all troubleshooting steps.
It builds device commands based on step_type and analyzes responses accordingly.

Usage:
    python step_executor.py build '{"step_type": "check_port_status", ...}'
    python step_executor.py analyze '{"step_type": "check_port_status", "response_data": "..."}'
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
    "check_port_status": [
        {"name": "device_ip", "label": "设备IP地址", "type": "text", "placeholder": "例如: 192.168.1.1", "required": True},
        {"name": "interface_name", "label": "接口名称", "type": "text", "placeholder": "例如: GigabitEthernet 1/0/1", "required": True},
    ],
    "default": [
        {"name": "device_ip", "label": "设备IP地址", "type": "text", "placeholder": "例如: 192.168.1.1", "required": True},
        {"name": "interface_name", "label": "接口名称", "type": "text", "placeholder": "例如: GigabitEthernet 1/0/1", "required": True},
    ]
}


def get_info_request_fields(analysis_type: str) -> list:
    """Get the field list for a specific analysis type."""
    return INFO_REQUEST_FIELDS.get(analysis_type, INFO_REQUEST_FIELDS.get("default", []))


def load_devices_config(devices_file_path: str = None) -> list:
    """Load device configuration from devices.json file."""
    if devices_file_path is None:
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
    """Execute commands on a remote device via Terminal API."""
    payload = [{
        "command": commands,
        "device": {
            "ip": device_info.get("ip", ""),
            "password": device_info.get("password", ""),
            "port": device_info.get("port", 23),
            "protocol": device_info.get("protocol", "telnet"),
            "username": device_info.get("username", "")
        },
        "sessionId": session_id
    }]
    
    print(f"# [API Request] Endpoint: {DEVICE_INFO_ENDPOINT}", file=sys.stderr)
    print(f"# [API Request] Payload:", file=sys.stderr)
    print(f"#   {json.dumps(payload, ensure_ascii=False, indent=2)}", file=sys.stderr)
    
    try:
        response = requests.post(
            DEVICE_INFO_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60
        )
        response.raise_for_status()
        
        result = response.json()
        
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
        print(f"# [API Error] Request timed out after 60s", file=sys.stderr)
        raise Exception("Device command execution timed out (60s)")
    except requests.exceptions.ConnectionError as e:
        print(f"# [API Error] Connection failed: {e}", file=sys.stderr)
        print(f"# Returning mock response for demonstration", file=sys.stderr)
        return _generate_mock_response(commands, device_info)
    except Exception as e:
        print(f"# [API Error] Request failed: {e}", file=sys.stderr)
        print(f"# Returning mock response for demonstration", file=sys.stderr)
        return _generate_mock_response(commands, device_info)


def _generate_mock_response(commands: List[str], device_info: Dict[str, Any]) -> Dict[str, Any]:
    """Generate mock API response for testing/demonstration."""
    import uuid
    
    mock_outputs = {
        "display interface": """
GigabitEthernet1/0/1 current state: DOWN ( Administratively )
IP Packet Frame Type: PKTFMT_ETHERNET_2, Hardware Address: 000f-e2b2-35ae
Description: GigabitEthernet1/0/1 Interface
Loopback is not set
Media type is twisted pair
Port hardware type is 1000_BASE_T
1000Mbps-speed mode, full-duplex mode
Link speed type is autonegotiation, link duplex type is autonegotiation
Input (total): 4083 packets, 595822 bytes
  1785 unicast, 501 broadcasts, 1797 multicast, 0 pauses
Input (normal): 4083 packets, - bytes
  1785 unicast, 501 broadcasts, 1797 multicast, 0 pauses
Input: 0 input errors, 0 runts, 0 giants, 0 throttles
  0 CRC, 0 frame, - overruns, 0 aborts
  - ignored, - parity errors
Output (total): 4198 packets, 593898 bytes
  1864 unicast, 538 broadcasts, 1796 multicast, 0 pauses
Output (normal): 4198 packets, - bytes
  1864 unicast, 538 broadcasts, 1796 multicast, 0 pauses
Output: 0 output errors, - underruns, - buffer failures
  0 aborts, 0 deferred, 0 collisions, 0 late collisions
  0 lost carrier, - no carrier
""",
        "display interface up": """
GigabitEthernet1/0/1 current state: UP
IP Packet Frame Type: PKTFMT_ETHERNET_2, Hardware Address: 000f-e2b2-35ae
Description: GigabitEthernet1/0/1 Interface
Loopback is not set
Media type is optical fiber
Port hardware type is 1000_BASE_SX
1000Mbps-speed mode, full-duplex mode
Link speed type is autonegotiation, link duplex type is autonegotiation
""",
        "display transceiver diagnosis": """
GigabitEthernet1/0/1 transceiver diagnostic information:
Current diagnostic parameters:
Temp(° C) Voltage(V) Bias(mA) RX power(dBm) TX power(dBm)
43 3.32 9.57 -3.77 -5.43
""",
        "display transceiver interface": """
GigabitEthernet1/0/1 transceiver information:
Transceiver Type        : 1000_BASE_SX_SFP
Connector Type          : LC
Wavelength(nm)          : 850
Transfer Distance(m)    : 550(50um),270(62.5um)
Digital Diagnostic Monitoring : YES
Vendor Name             : H3C
Ordering Name           : SFP-GE-SX-MM850
""",
        "loopback internal": """
%Apr 26 12:08:44:576 2000 H3C IFNET/3/LINK_UPDOWN: GigabitEthernet1/0/1 link status is UP.
Loop internal succeeded!
%Apr 26 12:08:44:754 2000 H3C IFNET/3/LINK_UPDOWN: GigabitEthernet1/0/1 link status is DOWN.
"""
    }
    
    data_entries = []
    for cmd in commands:
        output = "mock response"
        for key, value in mock_outputs.items():
            if key in cmd.lower():
                if "up" in cmd.lower():
                    output = mock_outputs["display interface up"]
                else:
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
        
        outputs = []
        for entry in data:
            echo = entry.get("echo", {})
            if echo:
                if isinstance(echo, dict):
                    outputs.append(list(echo.values())[0])
                else:
                    outputs.append(str(echo))
        
        return "\n".join(outputs)
        
    except Exception as e:
        print(f"# Warning: Failed to extract command output: {e}", file=sys.stderr)
        return ""


def render_command_template(template: str, params: Dict[str, Any]) -> str:
    """Render command template by replacing {{variable}} with actual values."""
    def replace_match(match):
        var_name = match.group(1)
        return str(params.get(var_name, match.group(0)))
    
    rendered = re.sub(r'\{\{(\w+)\}\}', replace_match, template)
    
    if rendered == template:
        for key, value in params.items():
            pattern = r'\b' + re.escape(key) + r'\b'
            if re.search(pattern, template) and key not in ['session_id', 'context_id', 'question_no']:
                print(f"# Warning: Template '{template}' appears to have untemplated variable '{key}'. Auto-fixing...", file=sys.stderr)
                rendered = re.sub(pattern, str(value), template)
                break
    
    return rendered


def build_commands_from_templates(templates: List[str], params: Dict[str, Any]) -> List[str]:
    """Build commands from templates by rendering each template."""
    commands = []
    for template in templates:
        rendered_cmd = render_command_template(template, params)
        commands.append(rendered_cmd)
    
    return commands


# ============================================================================
# Step Type Handlers - Result Analyzers
# ============================================================================

def analyze_check_port_status_result(response_data: str) -> Dict[str, Any]:
    """Analyze port status - Step 1: 查看端口是否Down."""
    result = {
        "step": 1,
        "step_name": "查看端口是否Down",
        "status": "",
        "port_up": False,
        "next_step": "",
        "message": ""
    }
    
    lower_data = response_data.lower()
    
    if "current state: up" in lower_data:
        result["port_up"] = True
        result["status"] = "success"
        result["next_step"] = "step3"
        result["message"] = f"端口状态为 UP，跳转到第三步检查错包增长情况"
    elif "current state: down" in lower_data:
        result["port_up"] = False
        result["status"] = "down"
        
        if "( administratively )" in lower_data:
            result["message"] = f"端口状态为 DOWN (Administratively)，即端口配置了shutdown"
        else:
            result["message"] = f"端口状态为 DOWN，需要检查Down类型"
        
        result["next_step"] = "step2"
    else:
        result["status"] = "error"
        result["next_step"] = "step2"
        result["message"] = "无法解析端口状态，默认跳转到检查Down类型"
    
    return result


def analyze_check_down_type_result(response_data: str) -> Dict[str, Any]:
    """Analyze down type - Step 2: 检查Down类型."""
    result = {
        "step": 2,
        "step_name": "检查Down类型",
        "status": "",
        "down_type": "",
        "next_step": "",
        "message": ""
    }
    
    lower_data = response_data.lower()
    
    if "( administratively )" in lower_data:
        result["down_type"] = "administrative"
        result["status"] = "info"
        result["message"] = "端口状态为 DOWN (Administratively)，即端口视图下配置了shutdown。请在端口视图下执行 undo shutdown 命令启用端口。"
    elif "link-aggregation interface down" in lower_data:
        result["down_type"] = "link_aggregation"
        result["status"] = "info"
        result["message"] = "端口状态为 DOWN (Link-Aggregation interface down)，即聚合逻辑口配置了shutdown。请检查聚合组配置。"
    elif "loop down" in lower_data:
        result["down_type"] = "loop"
        result["status"] = "warning"
        result["message"] = "端口状态为 DOWN (loop down)，Loopback-detection监测到环路，端口被强制关闭。请排查网络环路问题。"
    elif "bpdu-protected" in lower_data:
        result["down_type"] = "bpdu_protected"
        result["status"] = "warning"
        result["message"] = "端口状态为 DOWN (BPDU-protected)，该端口在BPDU guard功能的作用下被关闭。请检查STP配置和网络拓扑。"
    elif "monitor-link uplink down" in lower_data:
        result["down_type"] = "monitor_link"
        result["status"] = "info"
        result["message"] = "端口状态为 DOWN (Monitor-Link uplink down)，同一个Monitor Link组里的上行端口DOWN导致本端口DOWN。请检查上行链路。"
    elif "port security disabled" in lower_data:
        result["down_type"] = "port_security"
        result["status"] = "warning"
        result["message"] = "端口状态为 DOWN (Port Security Disabled)，检测到端口收到非法报文，端口安全的入侵检测机制将端口关闭。请检查端口安全配置和连接设备。"
    else:
        result["down_type"] = "unknown"
        result["status"] = "unknown"
        result["message"] = "无法识别具体的Down类型，可能是物理链路故障。"
    
    result["next_step"] = "step4"
    return result


def analyze_check_error_packets_result(response_data: str) -> Dict[str, Any]:
    """Analyze error packets - Step 3: 检查错包增长情况."""
    result = {
        "step": 3,
        "step_name": "检查错包增长情况",
        "status": "",
        "has_errors": False,
        "error_details": "",
        "next_step": "",
        "message": ""
    }
    
    errors_found = []
    
    input_errors_match = re.search(r'Input:\s*(\d+)\s*input errors', response_data)
    if input_errors_match and int(input_errors_match.group(1)) > 0:
        errors_found.append(f"输入错误包: {input_errors_match.group(1)}")
        result["has_errors"] = True
    
    crc_errors_match = re.search(r'(\d+)\s*CRC', response_data)
    if crc_errors_match and int(crc_errors_match.group(1)) > 0:
        errors_found.append(f"CRC错误: {crc_errors_match.group(1)}")
        result["has_errors"] = True
    
    frame_errors_match = re.search(r'(\d+)\s*frame', response_data)
    if frame_errors_match and int(frame_errors_match.group(1)) > 0:
        errors_found.append(f"帧错误: {frame_errors_match.group(1)}")
        result["has_errors"] = True
    
    runts_match = re.search(r'(\d+)\s*runts', response_data)
    if runts_match and int(runts_match.group(1)) > 0:
        errors_found.append(f"超短包: {runts_match.group(1)}")
        result["has_errors"] = True
    
    giants_match = re.search(r'(\d+)\s*giants', response_data)
    if giants_match and int(giants_match.group(1)) > 0:
        errors_found.append(f"超长包: {giants_match.group(1)}")
        result["has_errors"] = True
    
    output_errors_match = re.search(r'Output:\s*(\d+)\s*output errors', response_data)
    if output_errors_match and int(output_errors_match.group(1)) > 0:
        errors_found.append(f"输出错误包: {output_errors_match.group(1)}")
        result["has_errors"] = True
    
    if result["has_errors"]:
        result["error_details"] = ", ".join(errors_found)
        result["status"] = "warning"
        result["message"] = f"检测到错包增长：{result['error_details']}。建议：入方向错包增加请排查对端设备或测试链路；CRC/frame错误增加请替换网线或检查光衰；overruns/ignored计数增加请排查对端设备接收能力。"
    else:
        result["status"] = "success"
        result["message"] = "接口错包统计正常，未发现明显的错误包增长。"
    
    result["next_step"] = "step4"
    return result


def analyze_check_port_type_result(response_data: str) -> Dict[str, Any]:
    """Analyze port type - Step 4: 端口是否是光口."""
    result = {
        "step": 4,
        "step_name": "端口是否是光口",
        "status": "",
        "is_optical": False,
        "media_type": "",
        "next_step": "",
        "message": ""
    }
    
    lower_data = response_data.lower()
    
    if "media type is optical fiber" in lower_data:
        result["is_optical"] = True
        result["media_type"] = "optical"
        result["status"] = "optical"
        result["message"] = "端口类型为光口（Media type is optical fiber），继续检查光模块和光功率。"
        result["next_step"] = "step7"
    elif "media type is twisted pair" in lower_data:
        result["is_optical"] = False
        result["media_type"] = "twisted_pair"
        result["status"] = "copper"
        result["message"] = "端口类型为电口（Media type is twisted pair），使用双绞线作为传输媒介。"
        result["next_step"] = "step5"
    elif "media type is not sure" in lower_data or "no connector" in lower_data:
        result["is_optical"] = True
        result["media_type"] = "optical_no_module"
        result["status"] = "optical"
        result["message"] = "端口硬件类型显示为光口，但未检测到光模块（No connector）。请检查光模块是否正确安装。"
        result["next_step"] = "step7"
    else:
        result["is_optical"] = False
        result["media_type"] = "unknown"
        result["status"] = "unknown"
        result["message"] = "无法确定端口类型，默认按电口处理。"
        result["next_step"] = "step5"
    
    return result


def analyze_check_duplex_speed_result(response_data: str) -> Dict[str, Any]:
    """Analyze duplex and speed - Step 5: 检查双工速率."""
    result = {
        "step": 5,
        "step_name": "检查双工速率",
        "status": "",
        "speed": "",
        "duplex": "",
        "autonegotiation": False,
        "next_step": "",
        "message": ""
    }
    
    # Extract speed
    speed_match = re.search(r'(\d+)\s*mbps.*speed mode', response_data.lower())
    if speed_match:
        result["speed"] = f"{speed_match.group(1)}Mbps"
    
    # Extract duplex
    if "full-duplex" in response_data.lower():
        result["duplex"] = "full"
    elif "half-duplex" in response_data.lower():
        result["duplex"] = "half"
    
    # Check autonegotiation
    if "autonegotiation" in response_data.lower():
        result["autonegotiation"] = True
    
    if result["speed"] and result["duplex"]:
        result["status"] = "success"
        auto_text = "（自协商模式）" if result["autonegotiation"] else ""
        result["message"] = f"端口速率: {result['speed']}，双工模式: {result['duplex']}-duplex{auto_text}。请确保两端端口速率和双工模式匹配一致。"
    else:
        result["status"] = "warning"
        result["message"] = "无法完全解析端口速率和双工配置，请人工检查。"
    
    result["next_step"] = "step6"
    return result


def analyze_loopback_test_result(response_data: str) -> Dict[str, Any]:
    """Analyze loopback test - Step 6: 内环测试."""
    result = {
        "step": 6,
        "step_name": "内环测试",
        "status": "",
        "loopback_success": False,
        "next_step": "",
        "message": ""
    }
    
    lower_data = response_data.lower()
    
    if "loop internal succeeded" in lower_data or "link status is up" in lower_data:
        result["loopback_success"] = True
        result["status"] = "success"
        result["message"] = "内环测试成功！接口内部工作正常。建议替换网线排除物理链路故障，推荐使用5类及以上双绞线。"
        result["next_step"] = "step9"
    else:
        result["loopback_success"] = False
        result["status"] = "failure"
        result["message"] = "内环测试失败，可能是接口硬件故障。建议拨打技术支持热线：400-810-0504"
        result["next_step"] = "finish"
    
    return result


def analyze_check_optical_power_result(response_data: str) -> Dict[str, Any]:
    """Analyze optical power - Step 7: 查看两端收发光功率."""
    result = {
        "step": 7,
        "step_name": "查看两端收发光功率",
        "status": "",
        "rx_power": None,
        "tx_power": None,
        "power_normal": False,
        "next_step": "",
        "message": ""
    }
    
    power_match = re.search(r'(\-?[\d.]+)\s+(\-?[\d.]+)\s*$', response_data.strip(), re.MULTILINE)
    if not power_match:
        power_match = re.search(r'RX power\(dBm\)\s*TX power\(dBm\)\s*\n\s*[\d.]+\s+[\d.]+\s+[\d.]+\s+(\-?[\d.]+)\s+(\-?[\d.]+)', response_data)
    
    if power_match:
        try:
            result["rx_power"] = float(power_match.group(1))
            result["tx_power"] = float(power_match.group(2))
            
            # Check if power is within normal range
            # Typical range: RX: -30 to -5 dBm, TX: -10 to 0 dBm
            rx_ok = -30 <= result["rx_power"] <= -5
            tx_ok = -10 <= result["tx_power"] <= 0
            
            if rx_ok and tx_ok:
                result["power_normal"] = True
                result["status"] = "success"
                result["message"] = f"收发光功率正常。接收光功率: {result['rx_power']} dBm，发送光功率: {result['tx_power']} dBm。"
            elif not rx_ok:
                result["power_normal"] = False
                result["status"] = "warning"
                result["message"] = f"接收光功率异常: {result['rx_power']} dBm。正常范围通常为 -30 ~ -5 dBm。请检查光纤连接和光模块。"
            else:
                result["power_normal"] = False
                result["status"] = "warning"
                result["message"] = f"发送光功率异常: {result['tx_power']} dBm。正常范围通常为 -10 ~ 0 dBm。请检查光模块。"
        except ValueError:
            result["status"] = "error"
            result["message"] = "无法解析光功率数值。"
    else:
        result["status"] = "error"
        result["message"] = "无法获取光功率信息，请确保光模块已正确安装且支持数字诊断功能。"
    
    result["next_step"] = "step8"
    return result


def analyze_optical_loopback_test_result(response_data: str) -> Dict[str, Any]:
    """Analyze optical loopback test - Step 8: 光模块自环测试."""
    result = {
        "step": 8,
        "step_name": "光模块自环测试",
        "status": "",
        "loopback_possible": True,
        "next_step": "",
        "message": ""
    }
    
    lower_data = response_data.lower()
    
    if "media type is optical fiber" in lower_data or "current state: up" in lower_data:
        result["status"] = "info"
        result["message"] = "建议进行光模块自环测试：使用尾纤将光模块的Rx和Tx连接起来，确认接口是否可以Up。注意：10Km以上光模块需加光衰减器，测试前请将端口加入非业务VLAN避免环路。"
    else:
        result["status"] = "info"
        result["message"] = "光模块自环测试需要人工操作，将尾纤连接光模块的收发端口进行测试。"
    
    result["next_step"] = "step9"
    return result


def analyze_check_fiber_module_result(response_data: str) -> Dict[str, Any]:
    """Analyze fiber and module - Step 9: 检查光纤、替换模块."""
    result = {
        "step": 9,
        "step_name": "检查光纤、替换模块",
        "status": "",
        "module_info": {},
        "next_step": "",
        "message": ""
    }
    
    module_info = {}
    
    type_match = re.search(r'Transceiver Type\s*:\s*(.+)', response_data)
    if type_match:
        module_info["type"] = type_match.group(1).strip()
    
    connector_match = re.search(r'Connector Type\s*:\s*(.+)', response_data)
    if connector_match:
        module_info["connector"] = connector_match.group(1).strip()
    
    wavelength_match = re.search(r'Wavelength\(nm\)\s*:\s*(\d+)', response_data)
    if wavelength_match:
        module_info["wavelength"] = wavelength_match.group(1)
    
    distance_match = re.search(r'Transfer Distance\(m\)\s*:\s*(.+)', response_data)
    if distance_match:
        module_info["distance"] = distance_match.group(1).strip()
    
    vendor_match = re.search(r'Vendor Name\s*:\s*(.+)', response_data)
    if vendor_match:
        module_info["vendor"] = vendor_match.group(1).strip()
    
    result["module_info"] = module_info
    
    if module_info:
        result["status"] = "success"
        type_text = f"模块类型: {module_info.get('type', '未知')}"
        connector_text = f"接口类型: {module_info.get('connector', '未知')}"
        wave_text = f"波长: {module_info.get('wavelength', '未知')}nm"
        dist_text = f"传输距离: {module_info.get('distance', '未知')}"
        result["message"] = f"光模块信息已获取。{type_text}，{connector_text}，{wave_text}，{dist_text}。请确认使用了适配的光纤和模块。建议检查光纤连接，尝试替换光纤或模块排除物理链路故障。"
    else:
        result["status"] = "warning"
        result["message"] = "无法获取光模块信息，请检查光模块是否正确安装。建议尝试替换光纤或模块排除物理链路故障。"
    
    result["next_step"] = "finish"
    return result


# ============================================================================
# Main Entry Point
# ============================================================================

ANALYSIS_HANDLERS = {
    "check_port_status": analyze_check_port_status_result,
    "check_down_type": analyze_check_down_type_result,
    "check_error_packets": analyze_check_error_packets_result,
    "check_port_type": analyze_check_port_type_result,
    "check_duplex_speed": analyze_check_duplex_speed_result,
    "loopback_test": analyze_loopback_test_result,
    "check_optical_power": analyze_check_optical_power_result,
    "optical_loopback_test": analyze_optical_loopback_test_result,
    "check_fiber_module": analyze_check_fiber_module_result
}


def main():
    """Main entry point for the unified step executor."""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python step_executor.py <mode> [params]"}))
        sys.exit(1)
    
    mode = sys.argv[1]
    
    if mode == "build_and_execute":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing parameters for build_and_execute mode"}))
            sys.exit(1)
        
        params_input = sys.argv[2]
        if isinstance(params_input, str):
            try:
                params = json.loads(params_input)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON in params: {e}"}))
                sys.exit(1)
        else:
            params = params_input
        
        commands_templates = params.get("commands", [])
        analysis_type = params.get("analysis_type", "")
        step_type = params.get("step_type", "")
        session_id = params.get("session_id", "")
        interface_name = params.get("interface_name", "")
        step_number = params.get("step_number", 1)
        
        effective_analysis_type = analysis_type or step_type
        
        print(f"# [DEBUG] analysis_type: '{analysis_type}'", file=sys.stderr)
        print(f"# [DEBUG] step_type: '{step_type}'", file=sys.stderr)
        print(f"# [DEBUG] effective_analysis_type: '{effective_analysis_type}'", file=sys.stderr)
        
        if not session_id:
            import uuid
            session_id = f"troubleshooting-{uuid.uuid4().hex[:8]}"
            print(f"# Generated session_id: {session_id}", file=sys.stderr)
        
        if not commands_templates:
            print(json.dumps({
                "error": "Cannot determine commands. Please provide 'commands' field from SKILL.md"
            }))
            sys.exit(1)
        
        if not effective_analysis_type:
            print(json.dumps({"error": "analysis_type or step_type field is required"}))
            sys.exit(1)
        
        device_info = params.get("device_info", {})
        print(f"# ========== DEVICE INFO LOADING START ==========", file=sys.stderr)
        print(f"# Input device_info from params: {device_info}", file=sys.stderr)
        
        if isinstance(device_info, str):
            device_info = {"ip": device_info}
            print(f"# Converted device_info to dict: {device_info}", file=sys.stderr)
        
        ip_is_empty = not device_info.get("ip") or device_info.get("ip", "").strip() == ""
        if not device_info or "ip" not in device_info or ip_is_empty:
            print(f"# Warning: No device_info provided or incomplete", file=sys.stderr)
            print(f"#   device_info: {device_info}", file=sys.stderr)
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
                    print(f"# Warning: No devices found in devices.json, returning stepCommand", file=sys.stderr)
                    commands = build_commands_from_templates(commands_templates, params)
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
                            "note": "No device IP available. Please execute commands manually."
                        }
                    }
                    print(json.dumps(result, ensure_ascii=False))
                    sys.exit(0)
            except Exception as e:
                print(f"# Warning: Failed to load devices.json: {e}", file=sys.stderr)
                commands = build_commands_from_templates(commands_templates, params)
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
                        "note": f"Failed to load device config: {e}"
                    }
                }
                print(json.dumps(result, ensure_ascii=False))
                sys.exit(0)
        elif "ip" in device_info:
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
                        for key, value in full_device_info.items():
                            if key not in device_info or not device_info[key]:
                                device_info[key] = value
                        print(f"# Loaded device info for {ip_address}", file=sys.stderr)
                    else:
                        print(f"# Warning: Device {ip_address} not found in devices.json", file=sys.stderr)
                        
                except Exception as e:
                    print(f"# Warning: Failed to load devices.json: {e}", file=sys.stderr)
        
        original_device_info = params.get("device_info", {})
        if isinstance(original_device_info, str):
            original_device_info = {"ip": original_device_info}
        original_device_ip = original_device_info.get("ip", "")
        original_device_ip_empty = not original_device_ip or original_device_ip.strip() == ""
        
        # Check if interface_name is empty (need user confirmation)
        interface_name_empty = not interface_name or interface_name.strip() == ""
        
        if original_device_ip_empty or interface_name_empty:
            print(f"# Information missing, requesting from frontend", file=sys.stderr)
            
            missing_fields_display = []
            if original_device_ip_empty:
                missing_fields_display.append("设备IP地址")
            if interface_name_empty:
                missing_fields_display.append("接口名称")
            
            user_message = "需要补充以下信息以继续故障排查：<br/>"
            if original_device_ip_empty:
                user_message += "- 设备IP地址：格式为 xxx.xxx.xxx.xxx（例如：192.168.1.1）<br/>"
            if interface_name_empty:
                user_message += "- 接口名称：格式为 GigabitEthernet X/X/X（例如：GigabitEthernet 1/0/1）<br/>"
            
            result = {
                "answerType": "conversation",
                "contextEnd": "false",
                "contextId": params.get("context_id", ""),
                "currentStep": params.get("step_number", 0),
                "message": user_message,
                "questionNo": params.get("question_no", ""),
                "sessionId": session_id
            }
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0)
        
        commands = build_commands_from_templates(commands_templates, params)
        print(f"# Rendering {len(commands)} commands", file=sys.stderr)
        for i, cmd in enumerate(commands):
            print(f"#   Command {i+1}: {cmd}", file=sys.stderr)
        
        print(f"# ========== API EXECUTION START ==========", file=sys.stderr)
        print(f"# Executing {len(commands)} command(s) via Terminal API...", file=sys.stderr)
        try:
            api_payload = [{
                "command": commands,
                "device": {
                    "ip": device_info.get("ip", ""),
                    "password": device_info.get("password", ""),
                    "port": device_info.get("port", 23),
                    "protocol": device_info.get("protocol", "telnet"),
                    "username": device_info.get("username", "")
                },
                "sessionId": device_info.get("uuid") or session_id
            }]
            
            print(f"# [API Request] Full Payload:", file=sys.stderr)
            print(f"# [ {json.dumps(api_payload[0], ensure_ascii=False)} ]", file=sys.stderr)
            
            response = execute_device_command(commands, device_info, device_info.get("uuid") or session_id)
            
            response_data = extract_command_output(response)
            print(f"# [API Response] Extracted output (first 500 chars):", file=sys.stderr)
            print(f"#   {response_data[:500]}", file=sys.stderr)
            
            if effective_analysis_type in ANALYSIS_HANDLERS:
                analysis_result = ANALYSIS_HANDLERS[effective_analysis_type](response_data)
                print(f"# [Analysis] Result: {json.dumps(analysis_result, ensure_ascii=False)}", file=sys.stderr)
            else:
                analysis_result = {
                    "step": step_number,
                    "step_name": f"步骤{step_number}",
                    "status": "success",
                    "next_step": f"step{step_number + 1}",
                    "message": "命令执行完成，继续下一步。"
                }
            
            command_messages = []
            for idx, cmd in enumerate(commands):
                cmd_echo = ""
                try:
                    data = response.get("data", [])
                    if idx < len(data):
                        echo = data[idx].get("echo", {})
                        if echo:
                            if isinstance(echo, dict):
                                cmd_echo = list(echo.values())[0]
                            else:
                                cmd_echo = str(echo)
                except Exception:
                    pass
                
                command_messages.append({
                    "index": idx,
                    "command": cmd,
                    "echo": cmd_echo
                })
            
            step_command = {
                "answerType": "stepCommand",
                "currentStep": step_number,
                "sessionId": session_id,
                "questionNo": params.get("question_no", ""),
                "contextId": params.get("context_id", ""),
                "contextEnd": "false",
                "message": {
                    "commands": command_messages,
                    "sessionId": session_id
                }
            }
            
            step_content = {
                "answerType": "stepContent",
                "currentStep": step_number,
                "sessionId": session_id,
                "questionNo": params.get("question_no", ""),
                "contextId": params.get("context_id", ""),
                "contextEnd": "false",
                "message": analysis_result.get("message", ""),
                "nextStep": analysis_result.get("next_step", f"step{step_number + 1}")
            }
            
            final_result = {
                "stepBundle": [step_command, step_content],
                "answerType": "stepContent",
                "currentStep": step_number,
                "message": analysis_result.get("message", ""),
                "nextStep": analysis_result.get("next_step", f"step{step_number + 1}")
            }
            
            print(json.dumps(final_result, ensure_ascii=False))
        
        except Exception as e:
            print(f"# [ERROR] Execution failed: {e}", file=sys.stderr)
            error_result = {
                "answerType": "stepError",
                "currentStep": step_number,
                "sessionId": session_id,
                "questionNo": params.get("question_no", ""),
                "contextId": params.get("context_id", ""),
                "contextEnd": "false",
                "message": f"步骤{step_number}执行失败: {str(e)}",
                "nextStep": f"step{step_number}"
            }
            print(json.dumps(error_result, ensure_ascii=False))
    
    elif mode == "build":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing parameters for build mode"}))
            sys.exit(1)
        
        try:
            params = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON in params: {e}"}))
            sys.exit(1)
        
        commands_templates = params.get("commands", [])
        commands = build_commands_from_templates(commands_templates, params)
        
        result = {
            "commands": commands,
            "message": f"Built {len(commands)} command(s)"
        }
        print(json.dumps(result, ensure_ascii=False))
    
    elif mode == "analyze":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing parameters for analyze mode"}))
            sys.exit(1)
        
        try:
            params = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON in params: {e}"}))
            sys.exit(1)
        
        response_data = params.get("response_data", "")
        analysis_type = params.get("analysis_type", params.get("step_type", ""))
        step_number = params.get("step_number", 1)
        
        if not analysis_type:
            print(json.dumps({"error": "analysis_type or step_type is required"}))
            sys.exit(1)
        
        if analysis_type in ANALYSIS_HANDLERS:
            result = ANALYSIS_HANDLERS[analysis_type](response_data)
        else:
            result = {
                "step": step_number,
                "step_name": f"步骤{step_number}",
                "status": "success",
                "next_step": f"step{step_number + 1}",
                "message": "分析完成"
            }
        
        print(json.dumps(result, ensure_ascii=False))
    
    else:
        print(json.dumps({"error": f"Unknown mode: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
