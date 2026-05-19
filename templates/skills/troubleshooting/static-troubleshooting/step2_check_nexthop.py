"""
Step 2: Check next-hop IP reachability

This script encapsulates the ping command to verify next-hop reachability
and returns data in the format required by the frontend.
"""

import json
import sys
from typing import Dict, Any


def build_step_command(
    nexthop_ip: str,
    device_info: Dict[str, Any],
    context_id: str,
    question_no: str,
    session_id: str,
    current_step: int = 2
) -> Dict[str, Any]:
    """
    Build the step command data structure for frontend consumption.
    
    Args:
        nexthop_ip: Next-hop IP address to ping
        device_info: Device connection information
        context_id: Conversation context ID
        question_no: Question number/timestamp
        session_id: Session identifier
        current_step: Current step number (default: 2)
    
    Returns:
        Dictionary containing the step command data
    """
    # Build the ping command
    command = f"ping {nexthop_ip}"
    
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


def analyze_ping_result(response_data: str) -> Dict[str, Any]:
    """
    Analyze the ping response from the device.
    
    Args:
        response_data: Raw ping response from the device
    
    Returns:
        Analysis result with decision logic
    """
    result = {
        "step": 2,
        "step_name": "检查下一跳地址可达性",
        "status": "",
        "reachable": False,
        "packet_loss": 0,
        "next_step": "",
        "message": ""
    }
    
    # Parse ping results
    # H3C ping output typically includes success rate and packet loss
    if "success" in response_data.lower() or "100%" in response_data:
        # Successful ping
        result["reachable"] = True
        result["status"] = "success"
        result["next_step"] = "step3"
        result["message"] = "下一跳地址可达，继续检查路由掩码与最长匹配原则"
        
        # Try to extract packet loss percentage
        if "packet loss" in response_data.lower():
            try:
                # Look for pattern like "0.0% packet loss"
                import re
                match = re.search(r'(\d+\.?\d*)%\s*packet\s*loss', response_data, re.IGNORECASE)
                if match:
                    result["packet_loss"] = float(match.group(1))
            except:
                pass
    
    elif "timeout" in response_data.lower() or "unreachable" in response_data.lower():
        # Ping failed
        result["reachable"] = False
        result["status"] = "failed"
        result["next_step"] = "step8"
        result["message"] = "静态路由的下一跳地址不可达，请检查物理链路或直连网络配置"
        
        # Try to extract packet loss
        if "packet loss" in response_data.lower():
            try:
                import re
                match = re.search(r'(\d+\.?\d*)%\s*packet\s*loss', response_data, re.IGNORECASE)
                if match:
                    result["packet_loss"] = float(match.group(1))
            except:
                result["packet_loss"] = 100.0
        else:
            result["packet_loss"] = 100.0
    
    else:
        # Partial success or unclear result
        if "packet loss" in response_data.lower():
            try:
                import re
                match = re.search(r'(\d+\.?\d*)%\s*packet\s*loss', response_data, re.IGNORECASE)
                if match:
                    loss = float(match.group(1))
                    result["packet_loss"] = loss
                    
                    if loss < 50:
                        result["reachable"] = True
                        result["status"] = "partial"
                        result["next_step"] = "step3"
                        result["message"] = f"下一跳地址部分可达（丢包率 {loss}%），可能存在网络质量问题，建议继续排查"
                    else:
                        result["reachable"] = False
                        result["status"] = "poor"
                        result["next_step"] = "step8"
                        result["message"] = f"下一跳地址丢包严重（{loss}%），请检查网络质量"
            except:
                result["status"] = "error"
                result["next_step"] = "retry"
                result["message"] = "无法解析Ping结果，请重试"
        else:
            result["status"] = "error"
            result["next_step"] = "retry"
            result["message"] = "Ping命令执行失败或返回异常，请重试"
    
    return result


def main():
    """
    Main entry point for the script.
    
    Expected input via stdin or command line arguments:
    - Mode: "build" (build command) or "analyze" (analyze response)
    - Parameters as JSON
    """
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python step2_check_nexthop.py <mode> [params]"}))
        sys.exit(1)
    
    mode = sys.argv[1]
    
    if mode == "build":
        # Build step command
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing parameters for build mode"}))
            sys.exit(1)
        
        params = json.loads(sys.argv[2])
        
        step_command = build_step_command(
            nexthop_ip=params.get("nexthop_ip", ""),
            device_info=params.get("device_info", {}),
            context_id=params.get("context_id", ""),
            question_no=params.get("question_no", ""),
            session_id=params.get("session_id", ""),
            current_step=params.get("current_step", 2)
        )
        
        print(json.dumps(step_command, ensure_ascii=False))
    
    elif mode == "analyze":
        # Analyze response
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing response data for analyze mode"}))
            sys.exit(1)
        
        response_data = sys.argv[2]
        analysis_result = analyze_ping_result(response_data)
        
        print(json.dumps(analysis_result, ensure_ascii=False))
    
    else:
        print(json.dumps({"error": f"Unknown mode: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
