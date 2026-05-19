"""
Step 1: Check if static route exists in global routing table

This script encapsulates the device command to check the routing table
and returns data in the format required by the frontend.
"""

import json
import sys
from typing import Dict, Any


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
        
        step_command = build_step_command(
            destination_network=params.get("destination_network", ""),
            device_info=params.get("device_info", {}),
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
        
        response_data = sys.argv[2]
        analysis_result = analyze_route_result(response_data)
        
        print(json.dumps(analysis_result, ensure_ascii=False))
    
    else:
        print(json.dumps({"error": f"Unknown mode: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
