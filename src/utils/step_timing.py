"""Log de duracao por etapa (latencia). Nunca quebra o fluxo.

Uso: `with timed('llm.intake_turn', chat_id=...):` ou `@timed_async('x')`.
Formato: STEP_TIMING step=<nome> ms=<int> ok=<bool> k=v ...
Filtrar no log: grep STEP_TIMING. Etapas > STEP_TIMING_SLOW_MS saem em WARNING.
"""
import functools
import logging
import os
import time
from contextlib import contextmanager

logger = logging.getLogger("step_timing")
SLOW_MS = int(os.getenv("STEP_TIMING_SLOW_MS", "4000"))


def _emit(step, started, ok, fields):
    try:
        ms = int((time.perf_counter() - started) * 1000)
        extra = ' '.join(f'{k}={v}' for k, v in fields.items() if v is not None)
        logger.log(logging.WARNING if ms >= SLOW_MS else logging.INFO,
                   'STEP_TIMING step=%s ms=%d ok=%s %s', step, ms, ok, extra)
    except Exception:
        pass


@contextmanager
def timed(step, **fields):
    started, ok = time.perf_counter(), True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        _emit(step, started, ok, fields)


def timed_async(step):
    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*a, **kw):
            with timed(step):
                return await fn(*a, **kw)
        return inner
    return wrap
