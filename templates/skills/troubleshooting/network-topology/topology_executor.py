"""
Topology Executor for Network Topology Skill

This script fetches the network topology from a third-party API and
returns three pieces of frontend-bound messages so the agent can
sequentially push them to the user via SSE.

Usage:
    python topology_executor.py fetch '{"context_id": "...", "question_no": "...", "session_id": "..."}'

Returns (JSON):
    {
        "answerType": "topologyBundle",
        "sessionId": "...",
        "contextId": "...",
        "questionNo": "...",
        "currentStep": 0,
        "messages": [
            {"answerType": "conversation", "message": "网络拓扑图获取中...<br/>", ...},
            {"answerType": "topology", "message": {"nodes": [...], "edges": [...]}, ...},
            {"answerType": "conversation", "message": "获取成功", ...}
        ]
    }
"""

import json
import sys
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional


# 第三方拓扑接口配置
TOPOLOGY_API_URL = "http://127.0.0.1:20160/api/deviceTopology/get"
TOPOLOGY_REQUEST_TIMEOUT = 120  # seconds（拓扑获取比较慢，给到 2 分钟）


def load_devices_for_topology(devices_file_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """从 file/devices.json 加载设备列表，按拓扑接口要求的字段顺序透传。"""
    if devices_file_path is None:
        possible_paths = [
            Path(__file__).parent.parent.parent.parent.parent / "file" / "devices.json",
            Path(__file__).parent.parent.parent.parent.parent.parent / "file" / "devices.json",
            Path("f:/workspace/project/H3C/pythonclaw/file/devices.json"),
        ]
        devices_path = None
        for p in possible_paths:
            if p.exists():
                devices_path = p
                break
        if devices_path is None:
            raise FileNotFoundError(f"devices.json not found in: {possible_paths}")
    else:
        devices_path = Path(devices_file_path)

    # print(f"# [topology_executor] Loading devices from: {devices_path}", file=sys.stderr)
    with open(devices_path, "r", encoding="utf-8") as f:
        raw_devices = json.load(f)

    # 按 API 要求保证字段完整
    payload = []
    for d in raw_devices:
        payload.append({
            "ip": d.get("ip", ""),
            "userName": d.get("userName", ""),
            "password": d.get("password", ""),
            "port": d.get("port", 23),
            "protocol": d.get("protocol", "telnet"),
            "chassisId": d.get("chassisId", ""),
            "deviceName": d.get("deviceName", ""),
            "deviceCategory": d.get("deviceCategory", ""),
            "deviceModel": d.get("deviceModel", ""),
            "deviceId": d.get("deviceId", ""),
        })
    # print(f"# [topology_executor] Loaded {len(payload)} devices", file=sys.stderr)
    return payload


def fetch_topology(devices_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
    """调用第三方拓扑接口，返回 data.{nodes, edges} 内容；失败时抛异常或返回空结构。"""
    # print(f"# [topology_executor] Calling topology API: {TOPOLOGY_API_URL}", file=sys.stderr)
    # print(f"# [topology_executor] Request body (device count): {len(devices_payload)}", file=sys.stderr)

    response = requests.post(
        TOPOLOGY_API_URL,
        json=devices_payload,
        headers={"Content-Type": "application/json"},
        timeout=TOPOLOGY_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()

    # print(f"# [topology_executor] Response code: {body.get('code')}", file=sys.stderr)
    # print(f"# [topology_executor] Response message: {body.get('message')}", file=sys.stderr)

    if body.get("code") != 0:
        raise RuntimeError(f"Topology API error: {body.get('message', 'unknown error')}")

    data = body.get("data") or {}
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    # print(f"# [topology_executor] Topology nodes={len(nodes)} edges={len(edges)}", file=sys.stderr)
    return {"nodes": nodes, "edges": edges}


def build_frontend_messages(
    topology: Dict[str, Any],
    session_id: str,
    context_id: str,
    question_no: str,
) -> List[Dict[str, Any]]:
    """构造按需求约定顺序的三条前端消息。"""
    base = {
        "contextEnd": "false",
        "contextId": context_id,
        "currentStep": 0,
        "questionNo": question_no,
        "sessionId": session_id,
    }

    msg_start = {
        **base,
        "answerType": "conversation",
        "message": "网络拓扑图获取中...<br/>",
    }
    msg_topology = {
        **base,
        "answerType": "topology",
        "message": {
            "edges": topology.get("edges", []),
            "nodes": topology.get("nodes", []),
        },
    }
    msg_done = {
        **base,
        "answerType": "conversation",
        "message": "获取成功",
    }
    return [msg_start, msg_topology, msg_done]


def build_failure_messages(
    error_text: str,
    session_id: str,
    context_id: str,
    question_no: str,
) -> List[Dict[str, Any]]:
    """拓扑获取失败时仍然返回首尾两条 conversation 消息，避免前端流程中断。"""
    base = {
        "contextEnd": "false",
        "contextId": context_id,
        "currentStep": 0,
        "questionNo": question_no,
        "sessionId": session_id,
    }
    return [
        {**base, "answerType": "conversation", "message": "网络拓扑图获取中...<br/>"},
        {**base, "answerType": "conversation", "message": f"获取失败：{error_text}"},
    ]


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python topology_executor.py fetch '<params_json>'"}))
        sys.exit(1)

    mode = sys.argv[1]
    if mode != "fetch":
        print(json.dumps({"error": f"Unknown mode: {mode}. Use 'fetch'."}))
        sys.exit(1)

    params: Dict[str, Any] = {}
    if len(sys.argv) >= 3:
        try:
            params = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid params JSON: {e}"}))
            sys.exit(1)

    session_id = params.get("session_id", "") or params.get("sessionId", "")
    context_id = params.get("context_id", "") or params.get("contextId", "")
    question_no = params.get("question_no", "") or params.get("questionNo", "")

    try:
        devices_payload = load_devices_for_topology(params.get("devices_file_path"))
        topology = fetch_topology(devices_payload)
        messages = build_frontend_messages(topology, session_id, context_id, question_no)
        result = {
            "answerType": "topologyBundle",
            "sessionId": session_id,
            "contextId": context_id,
            "questionNo": question_no,
            "currentStep": 0,
            "status": "success",
            "messages": messages,
        }
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        messages = build_failure_messages(str(e), session_id, context_id, question_no)
        result = {
            "answerType": "topologyBundle",
            "sessionId": session_id,
            "contextId": context_id,
            "questionNo": question_no,
            "currentStep": 0,
            "status": "failed",
            "error": str(e),
            "messages": messages,
        }
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
