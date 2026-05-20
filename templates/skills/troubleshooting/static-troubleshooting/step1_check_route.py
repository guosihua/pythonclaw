"""
Step 1: Check if static route exists in global routing table

This script calls the terminal API to execute device commands and analyzes
the routing table response to determine if the static route exists.
"""

import json
import sys
import requests
from pathlib import Path
from typing import Dict, Any, Optional, List


# Terminal API Configuration
TERMINAL_API_BASE_URL = "http://10.153.61.64/terminal/api/terminal/ai"
DEVICE_INFO_ENDPOINT = f"{TERMINAL_API_BASE_URL}/deviceInfo"


def load_devices_config(devices_file_path: str = None) -> list:
    """
    Load device configuration from devices.json file.
    
    Args:
        devices_file_path: Path to devices.json file. If None, uses default path.
    
    Returns:
        List of device configurations
    
    Example:
        >>> devices = load_devices_config()
        >>> device = get_device_by_ip(devices, "10.88.142.204")
    """
    if devices_file_path is None:
        # Default path: project_root/file/devices.json
        project_root = Path(__file__).parent.parent.parent.parent.parent
        devices_file_path = project_root / "file" / "devices.json"
    
    devices_path = Path(devices_file_path)
    
    if not devices_path.exists():
        raise FileNotFoundError(f"Devices config file not found: {devices_path}")
    
    with open(devices_path, 'r', encoding='utf-8') as f:
        devices = json.load(f)
    
    return devices


def get_device_by_ip(devices: list, ip_address: str) -> Optional[Dict[str, Any]]:
    """
    Get device information by IP address.
    
    Args:
        devices: List of device configurations (from load_devices_config)
        ip_address: IP address to search for
    
    Returns:
        Device configuration dict or None if not found
    
    Example:
        >>> devices = load_devices_config()
        >>> device = get_device_by_ip(devices, "10.88.142.204")
        >>> if device:
        ...     print(f"Found device: {device['userName']}@{device['ip']}")
    """
    for device in devices:
        if device.get("ip") == ip_address:
            # Convert devices.json format to the format expected by build_step_command
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
    command: str,
    device_info: Dict[str, Any],
    session_id: str
) -> Dict[str, Any]:
    """
    Execute a command on a remote device via Terminal API.
    
    Args:
        command: The command to execute (e.g., "display ip routing-table 0.0.0.0/0")
        device_info: Device connection information
        session_id: Session identifier for the API call
    
    Returns:
        API response containing command output
    
    Raises:
        requests.exceptions.RequestException: If API call fails
    """
    # Build request payload
    payload = [{
        "command": [command],
        "device": {
            "ip": device_info.get("ip", ""),
            "password": device_info.get("password", ""),
            "port": device_info.get("port", 23),
            "protocol": device_info.get("protocol", "telnet"),
            "username": device_info.get("username", "")
        },
        "sessionId": session_id
    }]
    
    # Make API request
    try:
        response = requests.post(
            DEVICE_INFO_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        response.raise_for_status()
        
        result = response.json()
        
        # Check if API returned success
        if result.get("code") != 0:
            raise Exception(f"API error: {result.get('message', 'Unknown error')}")
        
        return result
        
    except requests.exceptions.Timeout:
        raise Exception("Device command execution timed out (30s)")
    except requests.exceptions.ConnectionError:
        raise Exception(f"Failed to connect to Terminal API at {DEVICE_INFO_ENDPOINT}")
    except Exception as e:
        raise Exception(f"Device command execution failed: {str(e)}")


def extract_command_output(api_response: Dict[str, Any]) -> str:
    """
    Extract the actual command output from API response.
    
    Args:
        api_response: Raw API response
    
    Returns:
        Extracted command output string
    """
    try:
        data = api_response.get("data", [])
        if not data:
            return ""
        
        # Get echo from first result
        first_result = data[0]
        echo = first_result.get("echo", {})
        
        # Echo is a dict mapping command to output
        # Example: {"display ip routing-table 0.0.0.0/0": "actual output..."}
        if echo:
            # Return the first (and usually only) value
            return list(echo.values())[0]
        
        return ""
        
    except Exception as e:
        print(f"# Warning: Failed to extract command output: {e}", file=sys.stderr)
        return ""


def build_step_command(
    destination_network: str,
    device_info: Dict[str, Any],
    context_id: str,
    question_no: str,
    session_id: str,
    current_step: int = 1
) -> Dict[str, Any]:
    """
    Build the step command data structure for frontend consumption.
    
    Args:
        destination_network: Target IP network (e.g., "192.168.1.0/24")
        device_info: Device connection information
        context_id: Conversation context ID
        question_no: Question number/timestamp
        session_id: Session identifier
        current_step: Current step number (default: 1)
    
    Returns:
        Dictionary containing the step command data
    """
    # Build the device command
    command = f"display ip routing-table {destination_network}"
    
    # Construct the response structure
    step_command = {
        "answerType": "stepCommand",
        "contextEnd": "false",
        "contextId": context_id,
        "currentStep": current_step,
        "message": {
            "deviceCommds": [
                {
                    "command": [command],
                    "device": {
                        "ip": device_info.get("ip", ""),
                        "password": device_info.get("password", ""),
                        "port": device_info.get("port", 23),
                        "protocol": device_info.get("protocol", "telnet"),
                        "username": device_info.get("username", ""),
                        "uuid": device_info.get("uuid", "")
                    }
                }
            ]
        },
        "questionNo": question_no,
        "sessionId": session_id
    }
    
    return step_command


def analyze_route_result(response_data: str) -> Dict[str, Any]:
    """
    Analyze the routing table response from the device.
    
    Args:
        response_data: Raw response from the device
    
    Returns:
        Analysis result with decision logic
    """
    result = {
        "step": 1,
        "step_name": "检查全局路由表中是否存在该静态路由",
        "status": "",
        "route_exists": False,
        "next_step": "",
        "message": ""
    }
    
    # Check if route exists in the response
    # H3C routing table typically shows routes in a tabular format
    if "Routing Table" in response_data or "Destinations" in response_data:
        # Look for the specific destination network
        # This is a simplified check - in production, you'd parse the full table
        if "Static" in response_data or "Direct" in response_data:
            result["route_exists"] = True
            result["status"] = "success"
            result["next_step"] = "step2"
            result["message"] = "路由表中存在该静态路由条目，继续检查下一跳可达性"
        else:
            result["route_exists"] = False
            result["status"] = "not_found"
            result["next_step"] = "step5"
            result["message"] = "路由表中未找到该静态路由，需要检查静态路由配置"
    else:
        # Error or no route found
        if "not found" in response_data.lower() or "no route" in response_data.lower():
            result["route_exists"] = False
            result["status"] = "not_found"
            result["next_step"] = "step5"
            result["message"] = "设备返回：路由不存在，跳转到检查静态路由配置"
        else:
            result["status"] = "error"
            result["next_step"] = "retry"
            result["message"] = "无法解析路由表回显，请重试或手动检查"
    
    return result


def main():
    """
    Main entry point for the script.
    
    Expected input via stdin or command line arguments:
    - Mode: "build" (build command) or "analyze" (analyze response)
    - Parameters as JSON
    
    For build mode, device_info can be:
    - Full device info dict (backward compatible)
    - Just IP address string (will load from devices.json)
    - Dict with only "ip" field (will merge with devices.json data)
    """
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python step1_check_route.py <mode> [params]"}))
        sys.exit(1)
    
    mode = sys.argv[1]
    
    if mode == "build":
        # Build step command
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing parameters for build mode"}))
            sys.exit(1)
        
        params = json.loads(sys.argv[2])
        
        # Load device information
        device_info = params.get("device_info", {})
        
        # If device_info is just an IP string, convert to dict
        if isinstance(device_info, str):
            device_info = {"ip": device_info}
        
        # If device_info only has IP, try to load full info from devices.json
        if device_info and "ip" in device_info:
            ip_address = device_info["ip"]
            
            # Check if we have complete device info or need to load from config
            needs_full_info = not all(k in device_info for k in ["username", "password", "port", "protocol"])
            
            if needs_full_info:
                try:
                    devices = load_devices_config()
                    full_device_info = get_device_by_ip(devices, ip_address)
                    
                    if full_device_info:
                        # Merge provided info with loaded info (provided takes precedence)
                        device_info = {**full_device_info, **device_info}
                        print(f"# Loaded device info for {ip_address} from devices.json", file=sys.stderr)
                    else:
                        print(f"# Warning: Device {ip_address} not found in devices.json, using provided info only", file=sys.stderr)
                        
                except FileNotFoundError as e:
                    print(f"# Warning: {e}, using provided device info only", file=sys.stderr)
                except Exception as e:
                    print(f"# Warning: Failed to load devices.json: {e}, using provided device info only", file=sys.stderr)
        
        step_command = build_step_command(
            destination_network=params.get("destination_network", ""),
            device_info=device_info,
            context_id=params.get("context_id", ""),
            question_no=params.get("question_no", ""),
            session_id=params.get("session_id", ""),
            current_step=params.get("current_step", 1)
        )
        
        print(json.dumps(step_command, ensure_ascii=False))
    
    elif mode == "analyze":
        # Analyze response
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing response data for analyze mode"}))
            sys.exit(1)
        
        # Parse the API response
        try:
            api_response = json.loads(sys.argv[2])
            
            # Extract command output from API response
            command_output = extract_command_output(api_response)
            
            if not command_output:
                print(json.dumps({
                    "step": 1,
                    "step_name": "检查全局路由表中是否存在该静态路由",
                    "status": "error",
                    "route_exists": False,
                    "next_step": "retry",
                    "message": "无法从API响应中提取命令输出"
                }, ensure_ascii=False))
                sys.exit(0)
            
            # Analyze the extracted output
            analysis_result = analyze_route_result(command_output)
            print(json.dumps(analysis_result, ensure_ascii=False))
            
        except json.JSONDecodeError as e:
            print(json.dumps({
                "step": 1,
                "step_name": "检查全局路由表中是否存在该静态路由",
                "status": "error",
                "route_exists": False,
                "next_step": "retry",
                "message": f"API响应JSON解析失败: {str(e)}"
            }, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({
                "step": 1,
                "step_name": "检查全局路由表中是否存在该静态路由",
                "status": "error",
                "route_exists": False,
                "next_step": "retry",
                "message": f"分析过程出错: {str(e)}"
            }, ensure_ascii=False))
    
    else:
        print(json.dumps({"error": f"Unknown mode: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
