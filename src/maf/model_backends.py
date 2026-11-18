import os
from typing import Protocol

from openai import APIError, OpenAI


class BackendError(RuntimeError):
    """A failed backend attempt that allows trying the next route."""


class Backend(Protocol):
    def complete(
        self, model: str | None, messages: list[dict], schema: dict, *, timeout: float | None = None
    ) -> str: ...


class OpenAICompatible:
    def __init__(self, base_url: str, api_key_env: str, timeout: float):
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.timeout = timeout

    def complete(self, model, messages, schema, *, timeout=None):
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise BackendError(f"missing {self.api_key_env}")

        try:
            with OpenAI(
                api_key=key, base_url=self.base_url,
                timeout=self.timeout if timeout is None else min(self.timeout, timeout),
                max_retries=0,
            ) as client:
                response = client.chat.completions.create(model=model, messages=messages)
        except APIError as error:
            raise BackendError(type(error).__name__) from None
        return (response.choices[0].message.content or "") if response.choices else ""
