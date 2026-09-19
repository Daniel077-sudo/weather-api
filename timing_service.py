import time
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional


_timing_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("timing_context", default=None)


def start_timing() -> Any:
    return _timing_context.set({})


def reset_timing(token: Any) -> None:
    _timing_context.reset(token)


def add_timing(name: str, ms: float) -> None:
    timing = _timing_context.get()
    if timing is None:
        return
    current = float(timing.get(name, 0.0) or 0.0)
    timing[name] = round(current + ms, 2)


def set_timing(name: str, value: Any) -> None:
    timing = _timing_context.get()
    if timing is not None:
        timing[name] = value


def get_timing() -> Dict[str, Any]:
    timing = _timing_context.get()
    return dict(timing or {})


@contextmanager
def timed(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        add_timing(name, (time.perf_counter() - started) * 1000)
