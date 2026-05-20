"""Test script to verify H3C AI API tool calling support."""

import asyncio
import json
import logging
from aiohttp import TCPConnector, ClientSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_h3c_tool_calling():
    """Test if H3C AI API supports function/tool calling."""
    
    # Authentication
    auth_url = "https://api-ai.h3c.com/session/api/user/login"
    api_url = "https://api-ai.h3c.com/session/ai/chat/deepseek"
    
    headers = {
        "Auth-Type": "DB",
        "Content-Type": "application/json",
    }
    payload = {"account": "ts_sn", "password": "ts_sn123"}
    
    connector = TCPConnector(ssl=False)
    async with ClientSession(connector=connector) as session:
        # Step 1: Get token
        logger.info("Step 1: Authenticating...")
        async with session.post(auth_url, headers=headers, json=payload) as resp:
            data = await resp.json()
            token = data.get("token")
            logger.info(f"Token obtained: {token[:20]}...")
        
        # Test multiple scenarios
        test_cases = [
            {
                "name": "Test 1: Simple tool call request",
                "user_message": "What's the weather in Beijing?",
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get the current weather in a location",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "location": {
                                        "type": "string",
                                        "description": "The city name"
                                    }
                                },
                                "required": ["location"]
                            }
                        }
                    }
                ],
                "tool_choice": "auto"
            },
            {
                "name": "Test 2: Force tool call with tool_choice='required'",
                "user_message": "Use get_weather to check Beijing weather",
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get the current weather in a location",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "location": {
                                        "type": "string",
                                        "description": "The city name"
                                    }
                                },
                                "required": ["location"]
                            }
                        }
                    }
                ],
                "tool_choice": "required"
            },
            {
                "name": "Test 3: Specific tool call",
                "user_message": "Check Beijing weather",
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get the current weather in a location",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "location": {
                                        "type": "string",
                                        "description": "The city name"
                                    }
                                },
                                "required": ["location"]
                            }
                        }
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "get_weather"}
                }
            }
        ]
        
        for test_case in test_cases:
            logger.info(f"\n{'='*60}")
            logger.info(test_case["name"])
            logger.info(f"{'='*60}")
            
            request_payload = {
                "chatInfo": {
                    "role": "user",
                    "content": test_case["user_message"]
                },
                "createSession": False,
                "multipleChat": False,
                "ip": "10.153.61.64",
                "requestSource": "",
                "stream": False,
                "sessionId": 0,
                "userId": "ts_sn",
                "isThink": True,
                "temperature": 0.2,
                "model": "DEEPSEEK_V3_PRIVATE",
                "tools": test_case["tools"]
            }
            
            # Add tool_choice if supported
            if test_case.get("tool_choice"):
                request_payload["tool_choice"] = test_case["tool_choice"]
                logger.info(f"Tool choice: {test_case['tool_choice']}")
            
            auth_headers = {
                "Authorization": token,
                "Content-Type": "application/json",
            }
            
            async with session.post(api_url, headers=auth_headers, json=request_payload) as resp:
                logger.info(f"Response status: {resp.status}")
                result = await resp.json()
                
                # Check for tool calls
                inner_data = result.get("data", {})
                message_obj = inner_data.get("message", {}) if isinstance(inner_data, dict) else {}
                
                tool_calls = message_obj.get("tool_calls")
                function_call = message_obj.get("function_call")
                content = message_obj.get("content", "")
                
                logger.info(f"Content length: {len(content)} chars")
                logger.info(f"Tool calls: {tool_calls}")
                logger.info(f"Function call: {function_call}")
                
                if tool_calls and tool_calls is not None:
                    logger.info(f"✅ SUCCESS: Tool calls detected!")
                    logger.info(json.dumps(tool_calls, indent=2, ensure_ascii=False))
                elif function_call and function_call is not None:
                    logger.info(f"✅ SUCCESS: Function call detected!")
                    logger.info(json.dumps(function_call, indent=2, ensure_ascii=False))
                else:
                    logger.info(f"❌ FAILED: No tool calls found")
                    logger.info(f"Content preview: {content[:200]}")

if __name__ == "__main__":
    asyncio.run(test_h3c_tool_calling())
