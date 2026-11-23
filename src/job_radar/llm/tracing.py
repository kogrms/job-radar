"""Langfuse traces for LLM calls. Tracing never fails a step: every error here is logged and swallowed."""

import logging
from typing import Any, Protocol

from langfuse import Langfuse

from job_radar.llm.provider import Usage

log = logging.getLogger(__name__)


class Generation(Protocol):
    def finish(
        self,
        *,
        output: Any,
        usage: Usage | None,
        cost_usd: float,
        error: str | None,
        scores: dict[str, float],
    ) -> None: ...


class Tracer(Protocol):
    def generation(
        self, *, name: str, model: str, input: Any, metadata: dict[str, Any], parameters: dict[str, Any]
    ) -> Generation: ...

    def flush(self) -> None: ...


class _NullGeneration:
    def finish(
        self,
        *,
        output: Any,
        usage: Usage | None,
        cost_usd: float,
        error: str | None,
        scores: dict[str, float],
    ) -> None:
        return None


class NullTracer:
    def generation(
        self, *, name: str, model: str, input: Any, metadata: dict[str, Any], parameters: dict[str, Any]
    ) -> Generation:
        return _NullGeneration()

    def flush(self) -> None:
        return None


class _LangfuseGeneration:
    def __init__(self, observation: Any) -> None:
        self._obs = observation

    def finish(
        self,
        *,
        output: Any,
        usage: Usage | None,
        cost_usd: float,
        error: str | None,
        scores: dict[str, float],
    ) -> None:
        if self._obs is None:
            return
        try:
            usage_details = (
                {
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "cached_input": usage.cached_input_tokens,
                    "reasoning": usage.reasoning_tokens,
                }
                if usage
                else None
            )
            self._obs.update(
                output=output,
                usage_details=usage_details,
                cost_details={"total": cost_usd},
                level="ERROR" if error else None,
                status_message=error,
            )
            for name, value in scores.items():
                self._obs.score(name=name, value=value)
            self._obs.end()
        except Exception:
            log.exception("langfuse: finishing a generation failed")


class LangfuseTracer:
    def __init__(self, client: Langfuse) -> None:
        self._lf = client

    def generation(
        self, *, name: str, model: str, input: Any, metadata: dict[str, Any], parameters: dict[str, Any]
    ) -> Generation:
        try:
            obs = self._lf.start_observation(
                name=name,
                as_type="generation",
                input=input,
                model=model,
                model_parameters=parameters,
                metadata=metadata,
            )
        except Exception:
            log.exception("langfuse: starting a generation failed")
            obs = None
        return _LangfuseGeneration(obs)

    def flush(self) -> None:
        try:
            self._lf.flush()
        except Exception:
            log.exception("langfuse: flush failed")
