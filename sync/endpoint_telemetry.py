"""Run-local, privacy-safe Garmin endpoint telemetry."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable, Iterator

from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError


_scope: ContextVar["EndpointTelemetry | None"] = ContextVar("garmin_endpoint_telemetry", default=None)


def is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    message = str(exc).lower()
    name = type(exc).__name__.lower()
    return "401" in message or "authentication" in message or "unauthorized" in message or "authentication" in name


class EndpointTelemetry:
    def __init__(self, run_kind: str) -> None:
        self.run_kind = run_kind
        self.started_at = datetime.now(timezone.utc)
        self._lock = threading.Lock()
        self._endpoints: dict[str, dict[str, int]] = {}

    def read(self, endpoint: str, call: Callable[[], Any]) -> Any:
        started = perf_counter()
        try:
            result = call()
        except GarminConnectTooManyRequestsError:
            outcome, empty = "rate_limits", False
            raise
        except Exception as exc:
            outcome, empty = ("authentication_failures" if is_auth_error(exc) else "failures"), False
            raise
        else:
            outcome, empty = "successes", isinstance(result, (list, dict)) and not result
            return result
        finally:
            elapsed = max(0, round((perf_counter() - started) * 1000))
            with self._lock:
                counters = self._endpoints.setdefault(endpoint, {
                    "calls": 0, "successes": 0, "valid_empty": 0, "failures": 0,
                    "authentication_failures": 0, "rate_limits": 0, "elapsed_ms": 0,
                })
                counters["calls"] += 1
                counters[outcome] += 1
                if empty:
                    counters["valid_empty"] += 1
                counters["elapsed_ms"] += elapsed

    def finish(self) -> dict[str, Any]:
        with self._lock:
            endpoints = {key: dict(value) for key, value in self._endpoints.items()}
        return {
            "run_kind": self.run_kind,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_calls": sum(item["calls"] for item in endpoints.values()),
            "endpoints": endpoints,
        }


def active() -> EndpointTelemetry | None:
    return _scope.get()


@contextmanager
def telemetry_scope(run_kind: str) -> Iterator[EndpointTelemetry]:
    collector = EndpointTelemetry(run_kind)
    token: Token = _scope.set(collector)
    try:
        yield collector
    finally:
        _scope.reset(token)


def instrument_read(endpoint: str, call: Callable[[], Any]) -> Any:
    collector = active()
    return collector.read(endpoint, call) if collector is not None else call()
