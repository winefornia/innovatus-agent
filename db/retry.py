"""Transient DB-connection error detection + retry for graph invocations.

The LangGraph Postgres checkpointer talks to Supabase through a psycopg pool.
Idle pooled connections get dropped by the pooler / cloud NAT, surfacing as
"SSL SYSCALL error", "consuming input failed", "server closed the connection",
etc. The pool's check=check_connection callback discards dead connections on
checkout, which handles the common case (stale connection at the start of a
request). This module is the belt-and-suspenders for the rarer case where a
connection dies *mid*-request: it retries the whole graph invocation.

Retrying a graph.invoke is safe here because every external mutation the graphs
make (Square customer/order/invoice/publish) uses a DETERMINISTIC idempotency
key derived from the case_id (see services.square_service._ikey), so a replayed
node re-issues the same request and Square deduplicates it — no double charge.
"""

import logging
import time

# Substrings that identify a dropped/transient Postgres connection, lower-cased.
_TRANSIENT_MARKERS = (
    "ssl syscall",
    "operation timed out",
    "connection reset",
    "server closed",
    "consuming input",
    "broken pipe",
    "could not receive data",
    "could not send data",
    "connection already closed",
    "the connection is closed",
    "connection is lost",
    "eof detected",
)


def is_transient_db_error(exc: BaseException) -> bool:
    """True if the exception looks like a dropped Postgres connection."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def invoke_with_retry(graph, *args, max_retries: int = 1, backoff: float = 0.5, **kwargs):
    """Call ``graph.invoke(*args, **kwargs)``, retrying on transient DB drops.

    Only transient connection errors are retried; everything else propagates
    immediately. Safe for resume/publish invocations because Square mutations
    are idempotent (deterministic idempotency keys).
    """
    last_exc: BaseException | None = None
    for attempt in range(1 + max_retries):
        try:
            return graph.invoke(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — re-raised below if not transient
            if is_transient_db_error(exc) and attempt < max_retries:
                logging.warning(
                    "[db-retry] transient DB error (attempt %d/%d), retrying: %s",
                    attempt + 1, max_retries, exc,
                )
                last_exc = exc
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    # Unreachable: the loop either returns or raises. Guard anyway.
    raise last_exc  # type: ignore[misc]
