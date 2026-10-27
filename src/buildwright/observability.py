from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .config import Settings


class NullTracer:
    """No-op: when Langfuse keys are absent — zero noise, zero active dependency."""

    @contextmanager
    def span(self, name: str, **meta: object) -> Iterator[None]:
        yield


class LangfuseTracer:
    def __init__(self, settings: Settings) -> None:
        from langfuse import Langfuse  # deferred import: only when actually used

        self._client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )

    @contextmanager
    def span(self, name: str, **meta: object) -> Iterator[object]:
        with self._client.start_as_current_observation(
            name=name, metadata=dict(meta)
        ) as span:
            yield span


def get_tracer(settings: Settings) -> NullTracer | LangfuseTracer:
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        try:
            return LangfuseTracer(settings)
        except Exception:  # noqa: BLE001 - observability must never break a run
            return NullTracer()
    return NullTracer()
