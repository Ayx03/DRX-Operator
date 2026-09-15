"""Model-aware output limits shared by Chat, Responses and native Messages."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from drx_agent.llm.base import LLMConfig

OPENAI_MAX_OUTPUT_TOKENS = 64000
CUSTOM_MODEL_MAX_OUTPUT_TOKENS = 16384

# Output capabilities, not context windows. Exact IDs avoid guessing future models.
_MODEL_OUTPUT_TOKENS = {
    "deepseek-chat": 8192,
    "deepseek-flash": 384000,
    "deepseek-v4-flash": 384000,
    "deepseek-v4-flash-vision-exp": 384000,
    "deepseek-v4-pro": 384000,
    "codex-mini-latest": 100000,
    "daybreak-blue-latest": 128000,
    "daybreak-red-latest": 128000,
    "gpt-4": 8192,
    "gpt-4-turbo": 4096,
    "gpt-4.1": 32768,
    "gpt-4.1-mini": 32768,
    "gpt-4.1-nano": 32768,
    "gpt-4o": 16384,
    "gpt-4o-2024-05-13": 4096,
    "gpt-4o-2024-08-06": 16384,
    "gpt-4o-2024-11-20": 16384,
    "gpt-4o-mini": 16384,
    "gpt-5": 128000,
    "gpt-5-chat-latest": 16384,
    "gpt-5-codex": 128000,
    "gpt-5-mini": 128000,
    "gpt-5-nano": 128000,
    "gpt-5-pro": 272000,
    "gpt-5.1": 128000,
    "gpt-5.1-chat-latest": 16384,
    "gpt-5.1-codex": 128000,
    "gpt-5.1-codex-max": 128000,
    "gpt-5.1-codex-mini": 128000,
    "gpt-5.2": 128000,
    "gpt-5.2-chat-latest": 16384,
    "gpt-5.2-codex": 128000,
    "gpt-5.2-pro": 128000,
    "gpt-5.3-chat-latest": 16384,
    "gpt-5.3-codex": 128000,
    "gpt-5.3-codex-spark": 32000,
    "gpt-5.4": 128000,
    "gpt-5.4-mini": 128000,
    "gpt-5.4-nano": 128000,
    "gpt-5.4-pro": 128000,
    "gpt-5.5": 128000,
    "gpt-5.5-pro": 128000,
    "gpt-5.6": 128000,
    "gpt-5.6-cyber": 128000,
    "gpt-5.6-luna": 128000,
    "gpt-5.6-luna-pro": 128000,
    "gpt-5.6-sol": 128000,
    "gpt-5.6-sol-pro": 128000,
    "gpt-5.6-terra": 128000,
    "gpt-5.6-terra-pro": 128000,
    "gpt-6-astra": 128000,
    "gpt-realtime-2.1": 32000,
    "o1": 100000,
    "o1-pro": 100000,
    "o3": 100000,
    "o3-deep-research": 100000,
    "o3-mini": 100000,
    "o3-pro": 100000,
    "o4-mini": 100000,
    "o4-mini-deep-research": 100000,
    "claude-3-5-sonnet-20240620": 8192,
    "claude-3-5-sonnet-20241022": 8192,
    "claude-3-haiku-20240307": 4096,
    "claude-fable-5": 128000,
    "claude-fable-5-1": 128000,
    "claude-haiku-4-5": 64000,
    "claude-haiku-4-5-20251001": 64000,
    "claude-mythos-5": 128000,
    "claude-mythos-5-1": 128000,
    "claude-opus-4-0": 32000,
    "claude-opus-4-1": 32000,
    "claude-opus-4-1-20250805": 32000,
    "claude-opus-4-20250514": 32000,
    "claude-opus-4-5": 64000,
    "claude-opus-4-5-20251101": 64000,
    "claude-opus-4-6": 128000,
    "claude-opus-4-7": 128000,
    "claude-opus-4-8": 128000,
    "claude-opus-5": 128000,
    "claude-sonnet-4-0": 64000,
    "claude-sonnet-4-20250514": 64000,
    "claude-sonnet-4-5": 64000,
    "claude-sonnet-4-5-20250929": 64000,
    "claude-sonnet-4-6": 128000,
    "claude-sonnet-5": 128000,
    "kimi-k2-0711-preview": 16384,
    "kimi-k2-0905-preview": 262144,
    "kimi-k2-thinking": 262144,
    "kimi-k2-thinking-turbo": 262144,
    "kimi-k2-turbo-preview": 262144,
    "kimi-k2.5": 262144,
    "kimi-k2.6": 262144,
    "kimi-k2.7-code": 262144,
    "kimi-k2.7-code-highspeed": 262144,
    "kimi-k3": 131072,
    "glm-4.5": 98304,
    "glm-4.5-air": 98304,
    "glm-4.5-flash": 98304,
    "glm-4.5v": 16384,
    "glm-4.6": 131072,
    "glm-4.6v": 32768,
    "glm-4.7": 131072,
    "glm-4.7-flash": 131072,
    "glm-4.7-flashx": 131072,
    "glm-5": 131072,
    "glm-5-turbo": 131072,
    "glm-5.1": 131072,
    "glm-5.2": 131072,
    "glm-5.3": 131072,
    "glm-5.3-flash": 131072,
    "glm-5v-turbo": 131072,
    "deepseek/deepseek-chat": 16000,
    "deepseek/deepseek-chat-v3-0324": 147456,
    "deepseek/deepseek-chat-v3.1": 32768,
    "deepseek/deepseek-r1": 16000,
    "deepseek/deepseek-r1-0528": 32768,
    "deepseek/deepseek-v3.1-terminus": 32768,
    "deepseek/deepseek-v3.1-terminus:exacto": 32768,
    "deepseek/deepseek-v3.2": 65536,
    "deepseek/deepseek-v3.2-exp": 65536,
    "deepseek/deepseek-v4-flash": 384000,
    "deepseek/deepseek-v4-flash-0731": 943718,
    "deepseek/deepseek-v4-flash-0731:batch": 943718,
    "deepseek/deepseek-v4-flash-vision-exp": 943718,
    "deepseek/deepseek-v4-flash-vision-exp:batch": 943718,
    "deepseek/deepseek-v4-flash:free": 384000,
    "deepseek/deepseek-v4-pro": 393216,
    "deepseek/deepseek-v4-pro-0813": 393216,
    "deepseek/deepseek-v4-pro-0813:batch": 943718,
    "deepseek/deepseek-v4.1-flash": 384000,
    "moonshotai/kimi-k2": 98304,
    "moonshotai/kimi-k2-0905": 98304,
    "moonshotai/kimi-k2-0905:exacto": 262144,
    "moonshotai/kimi-k2-thinking": 98304,
    "moonshotai/kimi-k2.5": 235929,
    "moonshotai/kimi-k2.6": 235929,
    "moonshotai/kimi-k2.6:free": 65536,
    "moonshotai/kimi-k2.7-code": 235929,
    "moonshotai/kimi-k2.7-code:batch": 262144,
    "moonshotai/kimi-k3": 943718,
    "moonshotai/kimi-k3:batch": 943718,
    "z-ai/glm-4-32b": 32768,
    "z-ai/glm-4.5": 98304,
    "z-ai/glm-4.5-air": 98304,
    "z-ai/glm-4.5-air:free": 96000,
    "z-ai/glm-4.5v": 16384,
    "z-ai/glm-4.6": 16384,
    "z-ai/glm-4.6:exacto": 131072,
    "z-ai/glm-4.6v": 32768,
    "z-ai/glm-4.7": 131072,
    "z-ai/glm-4.7-flash": 117964,
    "z-ai/glm-5": 128000,
    "z-ai/glm-5-turbo": 131072,
    "z-ai/glm-5.1": 128000,
    "z-ai/glm-5.2": 131072,
    "z-ai/glm-5.2:batch": 943718,
    "z-ai/glm-5.2:free": 230400,
    "z-ai/glm-5.3": 943717,
    "z-ai/glm-5.3-flash": 131072,
    "z-ai/glm-5.3-flash:batch": 943718,
    "z-ai/glm-5.3:batch": 943718,
    "z-ai/glm-5v-turbo": 131072,
}

_KIMI_CODE_OUTPUT_TOKENS = {
    "k3": 131072,
    "k3-256k": 131072,
    "kimi-for-coding": 32768,
    "kimi-for-coding-highspeed": 32768,
    "kimi-k2": 262144,
    "kimi-k2-turbo-preview": 32000,
    "kimi-k2.5": 65536,
}

_MAX_TOKENS_HOSTS = (
    "mistral.ai", "api.moonshot.ai", "api.kimi.com", "api.z.ai",
    "open.bigmodel.cn", "chutes.ai", "fireworks.ai", "api.deepseek.com",
)
_MAX_TOKENS_PROVIDERS = frozenset((
    "deepseek", "mistral", "moonshot", "kimi-code", "zai", "zhipu-coding-plan",
))
_FLASH_MODEL_CAPS = frozenset((
    "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
))
_ENDPOINT_PROVIDERS = {
    "api.deepseek.com": "deepseek", "api.openai.com": "openai",
    "api.anthropic.com": "anthropic", "openrouter.ai": "openrouter",
    "api.moonshot.ai": "moonshot", "api.kimi.com": "kimi-code",
    "api.z.ai": "zai", "open.bigmodel.cn": "zhipu-coding-plan",
    "ollama.com": "ollama",
}


def _provider_name(provider: str, base_url: str) -> str:
    provider = provider.lower()
    if provider not in ("", "openai_compatible", "openai-compatible"):
        return provider
    host = (urlparse(base_url).hostname or "").lower()
    return _ENDPOINT_PROVIDERS.get(host, provider)


def model_output_limit(model: str, provider: str = "", base_url: str = "") -> int | None:
    """Resolve a known output capability without inventing one for an opaque ID."""
    model = model.strip().lower()
    if model.startswith("@"):
        return None
    provider = _provider_name(provider, base_url)
    short_id = model.rsplit("/", 1)[-1]
    if provider == "kimi-code" and short_id in _KIMI_CODE_OUTPUT_TOKENS:
        return _KIMI_CODE_OUTPUT_TOKENS[short_id]
    return _MODEL_OUTPUT_TOKENS.get(model, _MODEL_OUTPUT_TOKENS.get(short_id))


def resolve_output_token_params(
    config: LLMConfig, *, api: str, base_url: str,
) -> dict[str, int]:
    """Apply endpoint omission, caller precedence, model caps and wire-field rules."""
    model = config.model.strip().lower().rsplit("/", 1)[-1]
    provider = _provider_name(config.provider, base_url)
    url = base_url.lower()
    capability = config.model_max_tokens
    if capability is None:
        capability = model_output_limit(config.model, provider, base_url)
    requested = config.max_tokens if config.max_tokens is not None else capability

    if api == "anthropic":
        ceiling = capability if capability is not None else OPENAI_MAX_OUTPUT_TOKENS
        return {"max_tokens": min(ceiling, requested if requested is not None else ceiling)}

    endpoint = urlparse(base_url)
    codex_path = endpoint.path.rstrip("/")
    is_codex = api == "responses" and (
        provider == "openai-codex"
        or (endpoint.scheme == "https" and endpoint.hostname == "chatgpt.com"
            and endpoint.port in (None, 443)
            and (codex_path == "/backend-api" or codex_path.startswith("/backend-api/")))
    )
    if is_codex or config.omit_max_output_tokens or provider == "ollama":
        return {}

    always_send = config.always_send_max_tokens
    if always_send is None:
        always_send = model.startswith("kimi-")
    if requested is None and always_send:
        requested = OPENAI_MAX_OUTPUT_TOKENS
    if requested is None:
        return {}
    is_openrouter = provider == "openrouter" or "openrouter.ai" in url
    if is_openrouter and not always_send and config.max_tokens is None:
        return {}

    model_clamp = config.clamp_output_to_model_max
    if model_clamp is None:
        model_clamp = (
            (provider == "deepseek" and model in _FLASH_MODEL_CAPS)
            or (provider in ("moonshot", "kimi-code") and model == "kimi-k3")
            or (provider in ("zai", "zhipu-coding-plan") and model in ("glm-5.2", "glm-5.3-flash"))
            or (api == "chat" and provider == "cline-pass")
        )
    ceiling = capability if model_clamp and capability is not None else OPENAI_MAX_OUTPUT_TOKENS
    value = min(requested, ceiling)
    if capability is not None:
        value = min(value, capability)
    if value <= 0:
        return {}
    if api == "responses":
        return {"max_output_tokens": value}
    field = config.max_tokens_field
    if field is None:
        field = "max_tokens" if (
            provider in _MAX_TOKENS_PROVIDERS or any(host in url for host in _MAX_TOKENS_HOSTS)
        ) else "max_completion_tokens"
    if field not in ("max_tokens", "max_completion_tokens"):
        raise ValueError("max_tokens_field must be max_tokens or max_completion_tokens")
    return {field: value}
