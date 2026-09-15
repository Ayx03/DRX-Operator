from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator
from enum import Enum

class AgentEventType(str, Enum):
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TEXT = "text"
    ERROR = "error"
    DONE = "done"

@dataclass
class AgentEvent:
    type: AgentEventType
    content: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

@dataclass
class LLMConfig:
    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7
    # None selects the model/endpoint default; a number requests an explicit cap.
    max_tokens: int | None = None
    # Which OpenAI-style API the model endpoint speaks: "chat" (chat/completions)
    # or "responses" (v1/responses). Providers that only speak one interface
    # ignore this; it exists for routing where both are possible.
    api_interface: str = "chat"
    # Output capability is separate from the context window and the requested cap.
    model_max_tokens: int | None = None
    omit_max_output_tokens: bool = False
    max_tokens_field: str | None = None
    always_send_max_tokens: bool | None = None
    clamp_output_to_model_max: bool | None = None
    provider: str = ""
    context_window: int | None = None

class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None, stream: bool = True) -> AsyncIterator[AgentEvent]:
        ...

    @abstractmethod
    def count_tokens(self, messages: list[dict]) -> int:
        ...

class LLMError(Exception):
    pass
