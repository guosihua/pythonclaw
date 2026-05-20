"""H3C internal AI platform provider.

Uses the company's internal deepseek API endpoint with token-based authentication.
Supports both streaming and non-streaming responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Generator
from typing import Any

import aiohttp
from aiohttp import TCPConnector

from .base import LLMProvider
from .response import MockChoice, MockFunction, MockMessage, MockResponse, MockToolCall

logger = logging.getLogger(__name__)


class H3CAIProvider(LLMProvider):
    """Provider for H3C internal AI platform."""

    def __init__(
        self,
        auth_url: str = "https://api-ai.h3c.com/session/api/user/login",
        api_endpoint: str = "https://api-ai.h3c.com/session/ai/chat/deepseek",
        model_name: str = "DEEPSEEK_V3_PRIVATE",
        account: str = "ts_sn",
        password: str = "ts_sn123",
    ) -> None:
        self.auth_url = auth_url
        self.api_endpoint = api_endpoint
        self.model_name = model_name
        self.account = account
        self.password = password
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._last_tool_calls = None  # Store tool calls from last response

    async def _get_token(self) -> str:
        """Get or refresh authentication token."""
        async with self._token_lock:
            if self._token:
                return self._token

            headers = {
                "Auth-Type": "DB",
                "Content-Type": "application/json",
            }
            payload = {"account": self.account, "password": self.password}

            connector = TCPConnector(ssl=False)
            try:
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.post(
                        self.auth_url,
                        headers=headers,
                        json=payload
                    ) as response:
                        data = await response.json()
                        if not response.ok:
                            raise ValueError(f"Authentication failed: {data}")
                        self._token = data.get("token")
                        logger.info("[H3CAI] Token obtained successfully")
                        return self._token
            except Exception as e:
                logger.error(f"[H3CAI] Failed to get token: {e}")
                raise

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> Any:
        """Send a chat request to H3C AI platform (synchronous wrapper)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._chat_async(messages, tools, tool_choice, stream=False, **kwargs)
            )
        finally:
            loop.close()

    async def _chat_async(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "auto",
        temperature: float = 0.2,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Async implementation of chat."""
        # Convert messages to the format expected by H3C API
        # Take the last user message as the prompt
        last_user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_msg = content
                elif isinstance(content, list):
                    # Handle multimodal content
                    for item in content:
                        if item.get("type") == "text":
                            last_user_msg = item.get("text", "")
                            break
                break

        payload = {
            "chatInfo": {"role": "user", "content": last_user_msg},
            "createSession": False,
            "multipleChat": False,
            "ip": "10.153.61.64",
            "requestSource": "",
            "stream": stream,  # Support streaming
            "sessionId": 0,
            "userId": self.account,
            "isThink": True,
            "temperature": temperature,
            "model": self.model_name,
        }
        
        # Add tools to payload if provided (for non-streaming requests)
        if tools:
            logger.info(f"[H3CAI] Converting {len(tools)} tools for non-streaming request")
            tools_payload = []
            for tool in tools:
                func_def = tool.get("function", {})
                h3c_tool = {
                    "type": "function",
                    "function": {
                        "name": func_def.get("name", ""),
                        "description": func_def.get("description", ""),
                        "parameters": func_def.get("parameters", {})
                    }
                }
                tools_payload.append(h3c_tool)
            payload["tools"] = tools_payload
            logger.info(f"[H3CAI] Tools included in non-streaming payload: {len(tools_payload)}")

        token = await self._get_token()
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

        connector = TCPConnector(ssl=False)
        try:
            logger.info(f"[H3CAI] Sending request to {self.api_endpoint}")
            logger.debug(f"[H3CAI] Request payload model: {payload.get('model')}")
            
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    self.api_endpoint,
                    headers=headers,
                    json=payload
                ) as response:
                    logger.info(f"[H3CAI] Response status: {response.status}")
                    
                    if not response.ok:
                        error_text = await response.text()
                        logger.error(f"[H3CAI] API request failed: {error_text}")
                        raise ValueError(f"API request failed: {error_text}")
                    
                    if stream:
                        # Handle streaming response
                        logger.info("[H3CAI] Processing streaming response")
                        return await self._handle_stream_response(response)
                    else:
                        # Handle non-streaming response
                        logger.info("[H3CAI] Processing non-streaming response")
                        result = await response.json()
                        logger.debug(f"[H3CAI] Response keys: {list(result.keys())}")
                        
                        # H3C API returns nested structure: {"code": 0, "msg": "...", "data": {...}}
                        # Extract content from data.message.content or data.message.reasoning_content
                        inner_data = result.get("data", {})
                        message_obj = inner_data.get("message", {}) if isinstance(inner_data, dict) else {}
                        
                        # Check for tool calls in non-streaming response
                        tool_calls = None
                        if "tool_calls" in message_obj or "function_call" in message_obj:
                            logger.info(f"[H3CAI] Detected tool calls in non-streaming response")
                            raw_tool_calls = message_obj.get("tool_calls", [])
                            if not raw_tool_calls and "function_call" in message_obj:
                                raw_tool_calls = [message_obj["function_call"]]
                            
                            if raw_tool_calls:
                                tool_calls = []
                                for i, tc in enumerate(raw_tool_calls):
                                    if isinstance(tc, dict):
                                        func_info = tc.get("function", tc)
                                        tool_call = MockToolCall(
                                            id=tc.get("id", f"call_{i}"),
                                            function=MockFunction(
                                                name=func_info.get("name", ""),
                                                arguments=json.dumps(func_info.get("arguments", {})) if isinstance(func_info.get("arguments"), dict) else func_info.get("arguments", "{}")
                                            ),
                                            type="function"
                                        )
                                        tool_calls.append(tool_call)
                                        logger.info(f"[H3CAI] Non-streaming tool call {i}: {func_info.get('name')}")
                                
                                logger.info(f"[H3CAI] Total non-streaming tool calls parsed: {len(tool_calls)}")
                        
                        # Try multiple possible content fields
                        content = (
                            message_obj.get('reasoning_content', '') or
                            message_obj.get('content', '') or
                            result.get("content", "")
                        )
                        
                        logger.info(f"[H3CAI] Response content length: {len(content)} chars")
                        
                        # Build mock response compatible with OpenAI format
                        message = MockMessage(
                            content=content,
                            tool_calls=tool_calls
                        )
                        
                        choice = MockChoice(
                            message=message
                        )
                        
                        return MockResponse(
                            choices=[choice]
                        )
        except Exception as e:
            logger.error(f"[H3CAI] Chat request failed: {e}")
            import traceback
            logger.error(f"[H3CAI] Traceback: {traceback.format_exc()}")
            raise

    async def _handle_stream_response(self, response) -> Any:
        """Handle streaming response from H3C API."""
        content_text = ""
        chunk_count = 0
        
        logger.info("[H3CAI] Starting to read streaming response")
        
        # Read streaming response line by line (SSE format)
        async for line in response.content:
            line = line.decode('utf-8').strip()
            if not line:
                continue
            
            chunk_count += 1
            if chunk_count <= 3 or chunk_count % 10 == 0:
                logger.debug(f"[H3CAI] Received chunk {chunk_count}: {line[:100]}")
            
            # Parse SSE format: data: {...}
            if line.startswith('data:'):
                data_str = line[5:].strip()
                if data_str == '[DONE]':
                    logger.info("[H3CAI] Received [DONE] marker")
                    break
                
                try:
                    data = json.loads(data_str)
                    # Extract content from streaming chunk
                    # Adjust this based on actual H3C API response format
                    delta_content = data.get('content', '') or data.get('delta', '') or data.get('text', '')
                    if delta_content:
                        content_text += delta_content
                        if len(content_text) % 50 == 0:
                            logger.debug(f"[H3CAI] Accumulated content length: {len(content_text)}")
                except json.JSONDecodeError:
                    logger.warning(f"[H3CAI] Failed to parse streaming chunk: {data_str}")
                    continue
        
        logger.info(f"[H3CAI] Streaming complete. Total chunks: {chunk_count}, Content length: {len(content_text)}")
        
        # Build final response
        message = MockMessage(
            content=content_text,
            tool_calls=None
        )
        
        choice = MockChoice(
            message=message
        )
        
        return MockResponse(
            choices=[choice]
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> Generator[dict[str, Any], None, Any]:
        """Stream chat responses using H3C API streaming support."""
        loop = asyncio.new_event_loop()
        try:
            # Get the async generator
            async_gen = self._chat_stream_async(messages, tools, tool_choice, **kwargs)
            
            # Manually iterate over the async generator
            while True:
                try:
                    # Run the next iteration of the async generator
                    chunk = loop.run_until_complete(async_gen.__anext__())
                    yield chunk
                except StopAsyncIteration:
                    # Build final response object to return via StopIteration.value
                    # This is required by Agent.chat_stream() to get tool_calls and other metadata
                    
                    # Use accumulated content from streaming
                    accumulated_content = getattr(self, '_accumulated_content', "")
                    
                    message = MockMessage(
                        content=accumulated_content,  # Use accumulated content
                        tool_calls=self._last_tool_calls  # Use captured tool calls from streaming
                    )
                    
                    choice = MockChoice(
                        message=message
                    )
                    
                    final_response = MockResponse(
                        choices=[choice]
                    )
                    
                    logger.info(f"[H3CAI] Returning final response object with {len(self._last_tool_calls or [])} tool calls, content length: {len(accumulated_content)} chars")
                    
                    # Reset for next request
                    self._last_tool_calls = None
                    self._accumulated_content = ""
                    
                    return final_response
        finally:
            loop.close()

    async def _chat_stream_async(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> Generator[dict[str, Any], None, Any]:
        """Async implementation of chat_stream."""
        # Convert messages to the format expected by H3C API
        last_user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_msg = content
                elif isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            last_user_msg = item.get("text", "")
                            break
                break

        logger.info(f"[H3CAI] Preparing stream request - Model: {self.model_name}, Message length: {len(last_user_msg)}")
        
        # Build tools definition for H3C API if provided
        tools_payload = None
        if tools:
            logger.info(f"[H3CAI] Converting {len(tools)} tools to H3C format")
            tools_payload = []
            for tool in tools:
                func_def = tool.get("function", {})
                h3c_tool = {
                    "type": "function",
                    "function": {
                        "name": func_def.get("name", ""),
                        "description": func_def.get("description", ""),
                        "parameters": func_def.get("parameters", {})
                    }
                }
                tools_payload.append(h3c_tool)
                if len(tools_payload) <= 3:
                    logger.debug(f"[H3CAI] Tool added: {func_def.get('name')}")
            logger.info(f"[H3CAI] Total tools converted: {len(tools_payload)}")
        
        payload = {
            "chatInfo": {"role": "user", "content": last_user_msg},
            "createSession": False,
            "multipleChat": False,
            "ip": "10.153.61.64",
            "requestSource": "",
            "stream": True,  # Enable streaming
            "sessionId": 0,
            "userId": self.account,
            "isThink": True,
            "temperature": kwargs.get("temperature", 0.2),
            "model": self.model_name,
        }
        
        # Add tools to payload if available
        if tools_payload:
            payload["tools"] = tools_payload
            logger.info(f"[H3CAI] Tools included in payload: {len(tools_payload)} tools")
        else:
            logger.warning("[H3CAI] No tools provided in request")

        token = await self._get_token()
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

        connector = TCPConnector(ssl=False)
        try:
            logger.info(f"[H3CAI] Sending stream request to {self.api_endpoint}")
            logger.debug(f"[H3CAI] Request payload: {json.dumps(payload, ensure_ascii=False)[:200]}...")
            
            # Reset tool call logging flags for this request
            self._tool_calls_logged = False
            self._tool_calls_count_logged = False
            self._null_tool_calls_logged = False
            
            # Accumulate all content for final response
            accumulated_content = []
            
            # Buffer for batching small chunks to improve efficiency
            # H3C API sends many tiny chunks (1-2 chars), we batch them to reduce yield frequency
            content_buffer = []
            
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    self.api_endpoint,
                    headers=headers,
                    json=payload
                ) as response:
                    logger.info(f"[H3CAI] Stream response status: {response.status}")
                    logger.info(f"[H3CAI] Response headers: {dict(response.headers)}")
                    
                    if not response.ok:
                        error_text = await response.text()
                        logger.error(f"[H3CAI] Stream API request failed: {error_text}")
                        raise ValueError(f"API request failed: {error_text}")
                    
                    # Process streaming response
                    logger.info("[H3CAI] Starting to read streaming response chunks")
                    chunk_count = 0
                    yielded_count = 0
                    tool_calls = None
                    
                    async for line in response.content:
                        chunk_count += 1
                        line = line.decode('utf-8').strip()
                        
                        if chunk_count <= 5 or chunk_count % 10 == 0:
                            logger.debug(f"[H3CAI] Received raw chunk {chunk_count}: {line[:150]}")
                        
                        if not line:
                            continue
                        
                        # Parse SSE format: data: {...}
                        if line.startswith('data:'):
                            data_str = line[5:].strip()
                            
                            if data_str == '[DONE]':
                                logger.info(f"[H3CAI] Received [DONE] marker after {chunk_count} chunks")
                                break
                            
                            try:
                                # H3C API returns nested structure: {"code": 0, "msg": "...", "data": {...}}
                                outer_data = json.loads(data_str)
                                
                                if chunk_count <= 3:
                                    logger.debug(f"[H3CAI] Parsed outer JSON keys: {list(outer_data.keys())}")
                                
                                # Extract the actual message data from nested structure
                                inner_data = outer_data.get("data", {})
                                if not inner_data:
                                    logger.debug(f"[H3CAI] No 'data' field in response")
                                    continue
                                
                                # Get message object
                                message_obj = inner_data.get("message", {})
                                if not message_obj:
                                    logger.debug(f"[H3CAI] No 'message' field in data")
                                    continue
                                
                                if chunk_count <= 3:
                                    logger.debug(f"[H3CAI] Message object keys: {list(message_obj.keys())}")
                                
                                # Check for tool calls in the response (only log once per request)
                                if "tool_calls" in message_obj or "function_call" in message_obj:
                                    raw_tool_calls = message_obj.get("tool_calls")
                                    if not raw_tool_calls and "function_call" in message_obj:
                                        # Single function call format
                                        raw_tool_calls = [message_obj["function_call"]]
                                    
                                    # Only process and log if we actually have tool calls
                                    if raw_tool_calls and raw_tool_calls is not None:
                                        if not hasattr(self, '_tool_calls_logged'):
                                            logger.info(f"[H3CAI] Detected tool calls in response")
                                            self._tool_calls_logged = True
                                        
                                        tool_calls = []
                                        for i, tc in enumerate(raw_tool_calls):
                                            if isinstance(tc, dict):
                                                func_info = tc.get("function", tc)
                                                tool_call = MockToolCall(
                                                    id=tc.get("id", f"call_{i}"),
                                                    function=MockFunction(
                                                        name=func_info.get("name", ""),
                                                        arguments=json.dumps(func_info.get("arguments", {})) if isinstance(func_info.get("arguments"), dict) else func_info.get("arguments", "{}")
                                                    ),
                                                    type="function"
                                                )
                                                tool_calls.append(tool_call)
                                                logger.info(f"[H3CAI] Tool call {i}: {func_info.get('name')}")
                                        
                                        if not hasattr(self, '_tool_calls_count_logged'):
                                            logger.info(f"[H3CAI] Total tool calls parsed: {len(tool_calls)}")
                                            self._tool_calls_count_logged = True
                                    else:
                                        # Log that tool_calls field exists but is null/empty (first time only)
                                        if chunk_count <= 5 and not hasattr(self, '_null_tool_calls_logged'):
                                            logger.debug(f"[H3CAI] tool_calls field present but value is: {raw_tool_calls}")
                                            self._null_tool_calls_logged = True

                                # Try multiple possible content fields in priority order
                                # H3C API uses 'reasoning_content' for streaming content
                                
                                # Debug: Log all content-related fields for the first few chunks
                                if chunk_count <= 5:
                                    logger.debug(f"[H3CAI] Chunk {chunk_count} - All message fields:")
                                    for key, value in message_obj.items():
                                        if value is not None and value != "":
                                            preview = str(value)[:100] if isinstance(value, str) else type(value).__name__
                                            logger.debug(f"[H3CAI]   {key}: {preview}")
                                
                                delta_content = (
                                    message_obj.get('reasoning_content', '') or  # Primary field for H3C
                                    message_obj.get('content', '') or
                                    message_obj.get('delta', '') or
                                    message_obj.get('text', '') or
                                    message_obj.get('answer', '') or
                                    message_obj.get('message', '')
                                )
                                
                                # Log detailed content information for debugging
                                if chunk_count <= 10 or yielded_count == 0:
                                    all_fields = {
                                        'reasoning_content': message_obj.get('reasoning_content'),
                                        'content': message_obj.get('content'),
                                        'delta': message_obj.get('delta'),
                                        'text': message_obj.get('text'),
                                        'answer': message_obj.get('answer'),
                                        'message': message_obj.get('message'),
                                    }
                                    non_empty = {k: v for k, v in all_fields.items() if v}
                                    logger.debug(f"[H3CAI] Chunk {chunk_count} - Content fields: {list(non_empty.keys())}")
                                    if non_empty:
                                        for field_name, field_value in list(non_empty.items())[:2]:
                                            preview = str(field_value)[:100] if field_value else ""
                                            logger.debug(f"[H3CAI]   {field_name}: '{preview}...'")
                                    elif chunk_count <= 10:
                                        logger.debug(f"[H3CAI] Chunk {chunk_count} - NO CONTENT FIELDS (all empty/null)")
                                
                                if delta_content:
                                    # Accumulate content for final response
                                    accumulated_content.append(delta_content)
                                    
                                    # Buffer small chunks to reduce yield frequency and improve efficiency
                                    # H3C API sends many tiny chunks (1-2 chars each), we should batch them
                                    content_buffer.append(delta_content)
                                    
                                    # Yield when buffer reaches threshold or at the end
                                    buffer_size = sum(len(c) for c in content_buffer)
                                    if buffer_size >= 50:  # Batch every 50 characters
                                        buffered_text = "".join(content_buffer)
                                        content_buffer.clear()
                                        
                                        yielded_count += 1
                                        if yielded_count <= 5 or yielded_count % 50 == 0:
                                            logger.debug(f"[H3CAI] Yielding batch {yielded_count} ({len(buffered_text)} chars): '{buffered_text[:80]}...'")
                                        yield {"type": "text_delta", "text": buffered_text}
                                else:
                                    # Skip empty chunks to avoid unnecessary processing
                                    # H3C API sends many empty chunks, we should ignore them
                                    if chunk_count <= 10:
                                        logger.debug(f"[H3CAI] Chunk {chunk_count} - Empty content, skipping")
                                    continue

                            except json.JSONDecodeError as e:
                                logger.warning(f"[H3CAI] Failed to parse streaming chunk: {data_str[:100]} - Error: {e}")
                                continue
                    
                    # Flush any remaining content in buffer before ending stream
                    if content_buffer:
                        buffered_text = "".join(content_buffer)
                        content_buffer.clear()
                        yielded_count += 1
                        logger.debug(f"[H3CAI] Flushing final buffer ({len(buffered_text)} chars): '{buffered_text[:80]}...'")
                        yield {"type": "text_delta", "text": buffered_text}
                    
                    logger.info(f"[H3CAI] Streaming complete. Total chunks received: {chunk_count}, Chunks yielded: {yielded_count}")

                    # Store accumulated content and tool_calls for final response object
                    self._accumulated_content = "".join(accumulated_content) if accumulated_content else ""
                    self._last_tool_calls = tool_calls
                    
                    logger.info(f"[H3CAI] Accumulated content length: {len(self._accumulated_content)} chars")
                    if self._accumulated_content:
                        logger.debug(f"[H3CAI] Accumulated content preview: '{self._accumulated_content[:200]}...'")
                    
        except Exception as e:
            logger.error(f"[H3CAI] Stream chat request failed: {e}")
            import traceback
            logger.error(f"[H3CAI] Traceback: {traceback.format_exc()}")
            raise
