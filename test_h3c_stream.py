"""Test script to check H3C AI API streaming response format."""

import asyncio
import json
import aiohttp
from aiohttp import TCPConnector


async def test_h3c_stream():
    """Test H3C AI streaming API and print raw response."""
    
    # Configuration - update these values
    ACCOUNT = "ts_sn"
    PASSWORD = "ts_sn123"  # Default password from H3CAIProvider
    API_ENDPOINT = "https://api-ai.h3c.com/session/ai/chat/deepseek"
    MODEL = "DEEPSEEK_V3_PRIVATE"
    
    # Step 1: Get token
    print("=" * 60)
    print("Step 1: Getting token...")
    print("=" * 60)
    
    login_url = "https://api-ai.h3c.com/session/api/user/login"
    login_payload = {
        "account": ACCOUNT,
        "password": PASSWORD
    }
    
    # IMPORTANT: H3C API requires Auth-Type header
    login_headers = {
        "Auth-Type": "DB",
        "Content-Type": "application/json",
    }
    
    token = None
    async with aiohttp.ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.post(login_url, headers=login_headers, json=login_payload) as response:
            print(f"Login status: {response.status}")
            login_result = await response.json()
            print(f"Login response: {json.dumps(login_result, ensure_ascii=False)[:200]}")
            
            if not response.ok:
                print("❌ Login failed!")
                return
            
            # Token might be in different locations depending on API version
            # Try multiple possible locations
            
            # Try direct token field
            token = login_result.get("token")
            
            # Try data.token
            if not token:
                data_obj = login_result.get("data", {})
                if isinstance(data_obj, dict):
                    token = data_obj.get("token")
            
            if not token:
                print("❌ No token in response!")
                print(f"Full response structure:")
                print(json.dumps(login_result, ensure_ascii=False, indent=2))
                return
            
            print(f"✅ Token obtained: {token[:50]}...")
    
    # Step 2: Test streaming API (create new session)
    print("\n" + "=" * 60)
    print("Step 2: Testing streaming API...")
    print("=" * 60)
    
    payload = {
        "chatInfo": {"role": "user", "content": "你好，请简单回复"},
        "createSession": False,
        "multipleChat": False,
        "ip": "10.153.61.64",
        "requestSource": "",
        "stream": True,
        "sessionId": 0,
        "userId": ACCOUNT,
        "isThink": True,
        "temperature": 0.2,
        "model": MODEL,
    }
    
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }
    
    print(f"Request URL: {API_ENDPOINT}")
    print(f"Request payload: {json.dumps(payload, ensure_ascii=False)}")
    print("\n--- Raw Response Lines ---\n")
    
    chunk_count = 0
    
    # Create a new session for the streaming request
    async with aiohttp.ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.post(API_ENDPOINT, headers=headers, json=payload) as response:
            print(f"Response status: {response.status}")
            print(f"Response headers: {dict(response.headers)}")
            print(f"Content-Type: {response.headers.get('Content-Type')}")
            print("\n")
            
            async for line in response.content:
                chunk_count += 1
                raw_line = line.decode('utf-8', errors='replace')
                
                print(f"[Chunk {chunk_count}] RAW: {repr(raw_line)}")
                
                # Try to parse as SSE
                if raw_line.strip().startswith('data:'):
                    data_str = raw_line.strip()[5:].strip()
                    print(f"         → SSE data: {data_str[:200]}")
                    
                    if data_str == '[DONE]':
                        print("         → [DONE] marker received")
                        break
                    
                    try:
                        data = json.loads(data_str)
                        print(f"         → Parsed JSON keys: {list(data.keys())}")
                        print(f"         → Full JSON: {json.dumps(data, ensure_ascii=False)[:300]}")
                    except json.JSONDecodeError as e:
                        print(f"         → ❌ JSON parse error: {e}")
                
                if chunk_count >= 20:  # Limit output
                    print("\n... (stopping after 20 chunks)")
                    break
    
    print(f"\n{'=' * 60}")
    print(f"Total chunks received: {chunk_count}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    print("H3C AI API Stream Format Test")
    print("This script will test the actual API response format\n")
    asyncio.run(test_h3c_stream())
