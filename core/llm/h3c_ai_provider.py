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
                        
                        # Parse the response and convert to OpenAI-compatible format
                        content = result.get("content", "")
                        logger.info(f"[H3CAI] Response content length: {len(content)} chars")
                        
                        # Build mock response compatible with OpenAI format
                        message = MockMessage(
                            content=content,
                            role="assistant",
                            tool_calls=None
                        )
                        
                        choice = MockChoice(
                            index=0,
                            message=message,
                            finish_reason="stop"
                        )
                        
                        return MockResponse(
                            choices=[choice],
                            model=self.model_name,
                            usage=None
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
            role="assistant",
            tool_calls=None
        )
        
        choice = MockChoice(
            index=0,
            message=message,
            finish_reason="stop"
        )
        
        return MockResponse(
            choices=[choice],
            model=self.model_name,
            usage=None
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
                    break
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

        token = await self._get_token()
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

        connector = TCPConnector(ssl=False)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    self.api_endpoint,
                    headers=headers,
                    json=payload
                ) as response:
                    if not response.ok:
                        error_text = await response.text()
                        raise ValueError(f"API request failed: {error_text}")
                    
                    # Process streaming response
                    async for line in response.content:
                        line = line.decode('utf-8').strip()
                        if not line:
                            continue
                        
                        # Parse SSE format: data: {...}
                        if line.startswith('data:'):
                            data_str = line[5:].strip()
                            if data_str == '[DONE]':
                                break
                            
                            try:
                                data = json.loads(data_str)
                                # Extract content from streaming chunk
                                # Adjust this based on actual H3C API response format
                                delta_content = data.get('content', '') or data.get('delta', '') or data.get('text', '')
                                if delta_content:
                                    yield {"type": "text_delta", "text": delta_content}
                            except json.JSONDecodeError:
                                logger.warning(f"[H3CAI] Failed to parse streaming chunk: {data_str}")
                                continue
        except Exception as e:
            logger.error(f"[H3CAI] Stream chat request failed: {e}")
            raise