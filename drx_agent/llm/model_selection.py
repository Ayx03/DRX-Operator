"""Configured-source model discovery and atomic, client-reusing selection."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from collections import Counter
from copy import copy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, unquote, urlsplit

from drx_agent.llm.base import LLMConfig, LLMProvider
from drx_agent.llm.output_tokens import CUSTOM_MODEL_MAX_OUTPUT_TOKENS, model_output_limit
from drx_agent.llm.resilient import ResilientProvider

_DISCOVERY_TIMEOUT = 10.0
_MODEL_ID = re.compile(r"[^\x00-\x1f\x7f]+\Z")
_SOURCE_LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_SELECTION_FIELDS = frozenset((
    "key", "provider", "model", "endpoint", "source_id", "context_window", "max_tokens",
))


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _capability(value: Any, names: tuple[str, ...]) -> int | None:
    for name in names:
        result = _positive_int(_field(value, name))
        if result is not None:
            return result
    return None


@dataclass(frozen=True)
class _Source:
    provider: LLMProvider
    label: str
    endpoint: str
    identity: str


@dataclass(frozen=True)
class _Model:
    source: _Source
    model: str
    context_window: int | None
    max_tokens: int

    @property
    def key(self) -> str:
        return f"{self.source.label}/{self.model}"

    def public(self, current: str | None) -> dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.source.label,
            "model": self.model,
            "endpoint": self.source.endpoint,
            "context_window": self.context_window,
            "max_tokens": self.max_tokens,
            "current": self.key == current,
        }


@dataclass(frozen=True)
class _PreparedSelection:
    owner: ModelSelection
    model: _Model
    provider: LLMProvider


class ModelSelection:
    """Own SDK client teardown; call ``close`` only after all consumers stop.

    Sources are captured once. Selection copies the provider and its config, not
    its SDK client, so old streams retain their model and EXO transport state.
    """

    def __init__(self, provider: ResilientProvider):
        self._provider = provider
        original = tuple(provider.providers)
        self._closed = False
        self._refresh_task: asyncio.Task[list[str]] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._current_key: str | None = None
        self._secrets: set[str] = set()
        urls = []
        for item in original:
            config: LLMConfig = getattr(item, "config")
            key = config.api_key
            if key:
                self._secrets.add(str(key))
            url = str(config.base_url or getattr(getattr(item, "client", None), "base_url", ""))
            urls.append(url)
            for address in (url, getattr(item, "control_url", "")):
                try:
                    parsed = urlsplit(address)
                    self._secrets.update(unquote(part) for part in (parsed.username, parsed.password) if part)
                    self._secrets.update(
                        value for name, value in parse_qsl(parsed.query)
                        if value and any(marker in name.casefold() for marker in (
                            "key", "token", "secret", "password", "credential", "auth", "sig",
                        ))
                    )
                except ValueError:
                    pass

        labels = []
        for item in original:
            config = getattr(item, "config")
            label = str(config.provider or "").strip().lower()
            if not _SOURCE_LABEL.fullmatch(label) or self._contains_secret(label):
                label = type(item).__name__.removesuffix("Provider").lower()
            if not _SOURCE_LABEL.fullmatch(label) or self._contains_secret(label):
                label = "provider"
            labels.append(label)
        totals = Counter(labels)
        occurrences: Counter[str] = Counter()
        self._sources: dict[str, _Source] = {}
        self._configured: dict[str, _Model] = {}
        for item, label, url in zip(original, labels, urls):
            config = getattr(item, "config")
            occurrences[label] += 1
            if totals[label] > 1:
                label = f"{label}#{occurrences[label]}"
            try:
                endpoint = urlsplit(url).hostname or ""
            except ValueError:
                endpoint = ""
            if self._contains_secret(endpoint):
                endpoint = ""
            # The opaque fingerprint prevents restoring a same-named source
            # after its endpoint/interface changes. No URL or credentials are
            # serialized, including URL paths, queries and user information.
            identity = hashlib.sha256("\0".join((
                type(item).__name__, str(config.provider),
                url, str(config.api_interface),
            )).encode()).hexdigest()
            source = _Source(item, label, endpoint, identity)
            self._sources[label] = source
            model = config.model
            if not self._valid_model(model):
                raise ValueError("A configured model has an unsupported identifier")
            row = _Model(
                source, model, _positive_int(config.context_window),
                _positive_int(config.model_max_tokens) or self._output_limit(source, model),
            )
            self._configured[label] = row
        self._models = {row.key: row for row in self._configured.values()}
        self._providers: dict[tuple[str, int | None, int], LLMProvider] = {}
        # Normalize the initial capability through a clone as well; never edit
        # a provider which a caller may already have handed to a stream.
        self.select(next(iter(self._models)))

    def _contains_secret(self, value: str) -> bool:
        folded = value.casefold()
        return any(
            secret.casefold() in folded if len(secret) >= 4 else secret.casefold() == folded
            for secret in self._secrets
        )

    def _valid_model(self, value: Any) -> bool:
        return (
            isinstance(value, str) and bool(value.strip()) and _MODEL_ID.fullmatch(value) is not None
            and "://" not in value and not self._contains_secret(value)
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("Model selection is closed")

    @staticmethod
    def _output_limit(source: _Source, model: str) -> int:
        config: LLMConfig = getattr(source.provider, "config")
        url = str(config.base_url or getattr(getattr(source.provider, "client", None), "base_url", ""))
        return model_output_limit(model, getattr(config, "provider", ""), url) or CUSTOM_MODEL_MAX_OUTPUT_TOKENS

    @property
    def current_key(self) -> str | None:
        return self._current_key

    def snapshot(self) -> list[dict[str, Any]]:
        return [model.public(self._current_key) for model in self._models.values()]

    def match(self, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        folded = query.casefold()
        exact_keys = [model.public(self._current_key) for key, model in self._models.items() if key.casefold() == folded]
        if exact_keys:
            return exact_keys
        exact = [model.public(self._current_key) for model in self._models.values() if model.model.casefold() == folded]
        if exact:
            return exact
        tokens = query.casefold().split()
        matches = []
        for model in self._models.values():
            searchable = f"{model.key} {model.source.endpoint}".casefold()
            if all(token in searchable for token in tokens):
                matches.append(model.public(self._current_key))
        return matches

    def _prepare(self, model: _Model) -> _PreparedSelection:
        cache_key = (model.key, model.context_window, model.max_tokens)
        chosen = self._providers.get(cache_key)
        if chosen is None:
            # Shallow-copying preserves provider-specific transport settings
            # (notably EXO control_url) while reusing the connection pool.
            try:
                chosen = copy(model.source.provider)
                config: LLMConfig = copy(getattr(model.source.provider, "config"))
                config.model = model.model
                config.context_window = model.context_window
                config.model_max_tokens = model.max_tokens
                setattr(chosen, "config", config)
            except Exception:
                raise ValueError("Unable to prepare the selected model") from None
            self._providers[cache_key] = chosen
        return _PreparedSelection(self, model, chosen)

    def commit_restore(self, prepared: _PreparedSelection) -> dict[str, Any]:
        """Commit a preflighted selection synchronously after the restore barrier."""
        self._ensure_open()
        if not isinstance(prepared, _PreparedSelection) or prepared.owner is not self:
            raise ValueError("Invalid prepared model selection")
        model = prepared.model
        # Replace the whole list: existing request snapshots keep their original
        # sequence. Other configured sources retain their original fallback order.
        self._provider.providers = [prepared.provider, *(
            source.provider for source in self._sources.values() if source is not model.source
        )]
        self._models[model.key] = model
        self._current_key = model.key
        return model.public(self._current_key)

    def select(self, key: str, *, allow_unlisted: bool = False) -> dict[str, Any]:
        self._ensure_open()
        if not isinstance(key, str):
            raise ValueError("Invalid model selector")
        model = self._models.get(key)
        if model is None:
            label, separator, identifier = key.partition("/")
            source = self._sources.get(label)
            if not allow_unlisted or not separator or source is None or not self._valid_model(identifier):
                raise ValueError("Model is not listed for a configured source")
            model = _Model(source, identifier, None, self._output_limit(source, identifier))
        return self.commit_restore(self._prepare(model))

    def export_selection(self) -> dict[str, Any] | None:
        if self._current_key is None:
            return None
        model = self._models[self._current_key]
        result = model.public(self._current_key)
        del result["current"]
        result["source_id"] = model.source.identity
        return result

    def prepare_restore(self, data: dict[str, Any]) -> _PreparedSelection:
        """Validate saved identity/capabilities without changing public state."""
        self._ensure_open()
        if not isinstance(data, dict) or set(data) != _SELECTION_FIELDS:
            raise ValueError("Invalid saved model selection")
        if not all(isinstance(data[name], str) for name in ("key", "provider", "model", "endpoint", "source_id")):
            raise ValueError("Invalid saved model identity")
        source = self._sources.get(data["provider"])
        if (
            source is None or data["source_id"] != source.identity
            or data["endpoint"] != source.endpoint or not self._valid_model(data["model"])
            or data["key"] != f"{source.label}/{data['model']}"
        ):
            raise ValueError("Saved model source is not configured")
        context = data["context_window"]
        capability = data["max_tokens"]
        if (context is not None and _positive_int(context) is None) or _positive_int(capability) is None:
            raise ValueError("Invalid saved model capabilities")
        # Configured or freshly discovered metadata wins over session data.
        # Otherwise the saved model can be restored offline on this source only.
        model = self._models.get(data["key"]) or _Model(source, data["model"], context, capability)
        return self._prepare(model)

    def restore_selection(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.commit_restore(self.prepare_restore(data))

    async def refresh(self) -> list[str]:
        self._ensure_open()
        task = self._refresh_task
        if task is None:
            task = asyncio.create_task(self._refresh())
            self._refresh_task = task
        try:
            return await task
        finally:
            if task.done() and self._refresh_task is task:
                self._refresh_task = None

    async def _list_models(self, source: _Source) -> tuple[dict[str, _Model], bool]:
        client = getattr(source.provider, "client", None)
        resource = getattr(client, "models", None)
        listing: Callable[[], Awaitable[Any]] | None = getattr(resource, "list", None)
        if not callable(listing):
            raise NotImplementedError
        page = await listing()
        rows: dict[str, _Model] = {}
        ignored = False
        while True:
            items = _field(page, "data")
            if not isinstance(items, (list, tuple)):
                raise ValueError("Invalid model discovery response")
            for item in items:
                identifier = _field(item, "id")
                if not self._valid_model(identifier):
                    ignored = True
                    continue
                context = _capability(item, (
                    "context_window", "context_length", "max_context_tokens", "max_input_tokens", "input_token_limit",
                ))
                capability = _capability(item, (
                    "max_output_tokens", "max_completion_tokens", "max_tokens", "output_token_limit",
                ))
                for name in ("top_provider", "limits", "capabilities"):
                    nested = _field(item, name)
                    context = context or _capability(nested, (
                        "context_window", "context_length", "max_context_tokens", "max_input_tokens",
                    ))
                    capability = capability or _capability(nested, (
                        "max_output_tokens", "max_completion_tokens", "max_tokens",
                    ))
                configured = self._configured[source.label]
                if identifier == configured.model:
                    context = configured.context_window or context
                    capability = configured.max_tokens
                row = _Model(source, identifier, context, capability or self._output_limit(source, identifier))
                rows[row.key] = row
            has_next = getattr(page, "has_next_page", None)
            if not callable(has_next) or not has_next():
                break
            page = await page.get_next_page()
        return rows, ignored

    async def _discover(self, source: _Source) -> tuple[dict[str, _Model] | None, list[str]]:
        try:
            rows, ignored = await asyncio.wait_for(self._list_models(source), _DISCOVERY_TIMEOUT)
        except asyncio.TimeoutError:
            return None, [f"{source.label}: model discovery timed out; configured choices remain available"]
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(exc, NotImplementedError) or status in (404, 405, 501):
                reason = "model listing is not supported"
            elif status in (401, 403):
                reason = "model discovery authorization failed"
            else:
                reason = "model discovery failed"
            # Never interpolate SDK errors: these can include headers, URLs,
            # response bodies or credentials from compatible endpoints.
            return None, [f"{source.label}: {reason}; configured choices remain available"]
        warnings = []
        if not rows:
            warnings.append(f"{source.label}: no models returned; configured choices remain available")
        if ignored:
            warnings.append(f"{source.label}: ignored unsupported model identifiers")
        return rows, warnings

    async def _refresh(self) -> list[str]:
        sources = tuple(self._sources.values())
        tasks = [asyncio.create_task(self._discover(source)) for source in sources]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ensure_open()
        current = self._models.get(self._current_key) if self._current_key is not None else None
        models: dict[str, _Model] = {}
        warnings = []
        for source, (discovered, failures) in zip(sources, results):
            warnings.extend(failures)
            if discovered is None:
                models.update((key, row) for key, row in self._models.items() if row.source is source)
            else:
                models.update(discovered)
            configured = self._configured[source.label]
            models.setdefault(configured.key, configured)
        # Discovery must not silently change an active request configuration.
        if current is not None:
            models[current.key] = current
        self._models = models
        return warnings

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        refresh = self._refresh_task
        if refresh is not None:
            refresh.cancel()
            await asyncio.gather(refresh, return_exceptions=True)
            self._refresh_task = None
        clients = {}
        for source in self._sources.values():
            client = getattr(source.provider, "client", None)
            if client is not None:
                clients[id(client)] = client
        for client in clients.values():
            try:
                result = client.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Teardown is best effort, with no credential-bearing SDK errors.
                pass
        self._providers.clear()
