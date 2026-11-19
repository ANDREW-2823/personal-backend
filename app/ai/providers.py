from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings as env_settings
from app.core.logging import get_logger
from app.schemas.core import AppSettingsSchema

logger = get_logger("jarvis.ai.providers")


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class BaseProvider:
    name = "base"

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ProviderResult:
        raise NotImplementedError

    async def stream_text(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        result = self.complete(messages, [])
        for index in range(0, len(result.content), 24):
            yield result.content[index : index + 24]


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


class OpenAIProvider(BaseProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = env_settings.ai_timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def complete(self, messages, tools) -> ProviderResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI request failed: {exc}") from exc
        data = response.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append(
                {
                    "id": call.get("id") or f"call_{len(calls)}",
                    "name": function.get("name"),
                    "arguments": _parse_arguments(function.get("arguments")),
                }
            )
        return ProviderResult(content=message.get("content") or "", tool_calls=calls)

    async def stream_text(self, messages) -> AsyncIterator[str]:
        payload = {"model": self.model, "messages": messages, "temperature": 0.3, "stream": True}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "ignore")
                        raise ProviderError(
                            f"OpenAI stream failed ({response.status_code}): {body[:300]}"
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                        text = delta.get("content")
                        if text:
                            yield text
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI stream failed: {exc}") from exc


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(
        self, api_key: str, model: str, base_url: str = "https://api.anthropic.com/v1"
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = env_settings.ai_timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _payload(self, messages, tools, stream: bool = False) -> dict[str, Any]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        conversation = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in {"user", "assistant"} and m.get("content")
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1200,
            "messages": conversation or [{"role": "user", "content": "Hello"}],
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"]["description"],
                    "input_schema": tool["function"]["parameters"],
                }
                for tool in tools
            ]
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, messages, tools) -> ProviderResult:
        try:
            response = httpx.post(
                f"{self.base_url}/messages",
                headers=self._headers(),
                json=self._payload(messages, tools),
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc
        data = response.json()
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                calls.append(
                    {
                        "id": block.get("id") or f"call_{len(calls)}",
                        "name": block.get("name"),
                        "arguments": block.get("input") or {},
                    }
                )
        return ProviderResult(content="".join(text_parts), tool_calls=calls)

    async def stream_text(self, messages) -> AsyncIterator[str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/messages",
                    headers=self._headers(),
                    json=self._payload(messages, [], stream=True),
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "ignore")
                        raise ProviderError(
                            f"Anthropic stream failed ({response.status_code}): {body[:300]}"
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "content_block_delta":
                            text = (event.get("delta") or {}).get("text")
                            if text:
                                yield text
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic stream failed: {exc}") from exc


class OllamaProvider(BaseProvider):
    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434") -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = env_settings.ai_timeout_seconds

    def _payload(self, messages, tools, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "stream": stream,
            "options": {"temperature": 0.3},
        }
        if tools:
            payload["tools"] = tools
        return payload

    def complete(self, messages, tools) -> ProviderResult:
        try:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json=self._payload(messages, tools, False),
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama request failed: {exc}") from exc
        message = response.json().get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append(
                {
                    "id": f"call_{len(calls)}",
                    "name": function.get("name"),
                    "arguments": _parse_arguments(function.get("arguments")),
                }
            )
        return ProviderResult(content=message.get("content") or "", tool_calls=calls)

    async def stream_text(self, messages) -> AsyncIterator[str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/chat",
                    json=self._payload(messages, [], True),
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "ignore")
                        raise ProviderError(f"Ollama stream failed: {body[:300]}")
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        text = (chunk.get("message") or {}).get("content")
                        if text:
                            yield text
                        if chunk.get("done"):
                            break
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama stream failed: {exc}") from exc


def build_provider(app_settings: AppSettingsSchema) -> tuple[BaseProvider | None, str]:
    """Return (provider, mode-note). None means the offline assistant is used."""
    provider = app_settings.ai_provider
    if provider == "mock":
        return None, "Offline assistant (no AI credentials configured)"
    if provider == "openai":
        if not env_settings.ai_api_key:
            return None, "OpenAI selected but AI_API_KEY is missing; using offline assistant"
        return OpenAIProvider(
            env_settings.ai_api_key, app_settings.ai_model, env_settings.ai_base_url
        ), "OpenAI"
    if provider == "anthropic":
        if not env_settings.ai_api_key:
            return None, "Anthropic selected but AI_API_KEY is missing; using offline assistant"
        return AnthropicProvider(env_settings.ai_api_key, app_settings.ai_model), "Anthropic"
    if provider == "ollama":
        return OllamaProvider(app_settings.ai_model, env_settings.ollama_base_url), "Ollama"
    return None, "Unknown provider; using offline assistant"
