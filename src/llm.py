"""Native OpenAI transport shared by domain services (no agent framework).

Messages use the provider's JSON wire contract. ChatResult is the small domain
result consumed by the existing usage tracker and knowledge-base tool runner.
"""
from dataclasses import dataclass, field
import copy
import json
from typing import Any
from openai import AsyncOpenAI, OpenAI
from config import llm_default_headers


def system_message(content, **kwargs):
    return {"role": "system", "content": content, **kwargs}


def user_message(content, **kwargs):
    return {"role": "user", "content": content, **kwargs}


def assistant_message(content, **kwargs):
    return {"role": "assistant", "content": content, **kwargs}


def tool_message(content, tool_call_id, **kwargs):
    return {"role": "tool", "content": content, "tool_call_id": tool_call_id, **kwargs}


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage_metadata: dict = field(default_factory=dict)
    response_metadata: dict = field(default_factory=dict)
    wire_message: dict = field(default_factory=dict)


class ChatModel:
    """Explicit native SDK transport with tools and schema-validated responses."""

    def __init__(self, model, api_key=None, openai_api_key=None,
                 temperature=None, timeout=60, request_timeout=None,
                 max_retries=1, max_tokens=None, model_kwargs=None, **kwargs):
        self.model = self.model_name = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.options = dict(model_kwargs or {})
        self.options.update(kwargs)
        self.client_options = {"api_key": api_key or openai_api_key,
                               "timeout": request_timeout or timeout,
                               "max_retries": max_retries,
                               "default_headers": llm_default_headers()}
        self._async_client = self._sync_client = None
        self.tools = None
        self.schema = None

    def with_tools(self, tools):
        model = copy.copy(self)
        model.tools = tools
        return model

    def with_structured_output(self, schema, **kwargs):
        model = copy.copy(self)
        model.schema = schema
        return model

    def _request(self, messages):
        if isinstance(messages, str):
            messages = [user_message(messages)]
        wire = [m.wire_message if isinstance(m, ChatResult) else dict(m) for m in messages]
        body = {"model": self.model, "messages": wire, **self.options}
        # Reasoning models reject sampling parameters, even their defaults.
        reasoning = self.model.startswith(("gpt-5", "o1", "o3", "o4"))
        if self.temperature is not None and not reasoning:
            body["temperature"] = self.temperature
        if self.max_tokens:
            body["max_completion_tokens"] = self.max_tokens
        if self.tools:
            body["tools"] = self.tools
        return body

    def _result(self, response):
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise ValueError("Model refused the requested response")
        if response.choices[0].finish_reason == "length":
            raise ValueError("Model response was truncated")
        if self.schema is not None:
            parsed = getattr(message, "parsed", None)
            if parsed is None:
                raise ValueError("Model did not return the required structured response")
            return parsed
        usage = response.usage.model_dump() if response.usage else {}
        calls = [{"id": c.id, "name": c.function.name,
                  "args": json.loads(c.function.arguments)} for c in message.tool_calls or []]
        return ChatResult(
            content=message.content or "", tool_calls=calls,
            usage_metadata={"input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0)},
            response_metadata={"token_usage": usage, "model_name": response.model},
            wire_message={k: v for k, v in message.model_dump(exclude_none=True).items()
                          if k in {'role', 'content', 'tool_calls', 'refusal'}},
        )

    async def complete(self, messages):
        if self._async_client is None:
            self._async_client = AsyncOpenAI(**self.client_options)
        body = self._request(messages)
        if self.schema is not None:
            response = await self._async_client.chat.completions.parse(**body, response_format=self.schema)
        else:
            response = await self._async_client.chat.completions.create(**body)
        return self._result(response)

    def complete_sync(self, messages):
        if self._sync_client is None:
            self._sync_client = OpenAI(**self.client_options)
        body = self._request(messages)
        if self.schema is not None:
            response = self._sync_client.chat.completions.parse(**body, response_format=self.schema)
        else:
            response = self._sync_client.chat.completions.create(**body)
        return self._result(response)

    async def close(self):
        if self._async_client is not None:
            await self._async_client.close()
        if self._sync_client is not None:
            self._sync_client.close()
