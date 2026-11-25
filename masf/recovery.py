"""Bounded recovery decisions, persisted with the job before another attempt."""

import time

from .system import clean


class RecoverableFailure(RuntimeError):
    pass


OPERATOR_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "invalid api key",
    "authentication",
    "http 401",
    "permission denied",
    "ruta protegida",
)


def reserve(job, cfg, action, **detail):
    history = list(job.get("recoveries", []))
    limit = cfg["service"].get("max_recovery_attempts", 2)
    if len(history) >= limit:
        raise RuntimeError("Recuperaciones automáticas agotadas")
    conflict = job.get("pr_conflict") or {}
    inputs = {key: conflict[key] for key in ("pr", "base", "head", "fingerprint", "paths")
              if key in conflict}
    return [*history, {"action": action, "at": time.time(), **inputs, **detail}]


def wait(cfg, history, stage, minimum_delay=0):
    delay = cfg["service"].get("recovery_backoff_seconds", 30) * 2 ** (len(history) - 1)
    return {"stage": stage, "until": time.time() + max(delay, minimum_delay)}


def retryable(exc):
    """Classify known failures without treating every external error as transient."""
    from .system import CommandTimeout, ProcessError

    text = clean(str(exc))
    if any(marker in text.lower() for marker in OPERATOR_MARKERS):
        return False
    transient = isinstance(exc, (CommandTimeout, RecoverableFailure))
    if isinstance(exc, ProcessError):
        transient = any(
            marker in text.lower()
            for marker in (
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "connection reset",
                "connection refused",
                "temporary failure",
                "could not resolve host",
                "tls handshake timeout",
                "cannot connect to the docker daemon",
                "is unhealthy",
                "context deadline exceeded",
            )
        )
    return transient


def schedule(job, cfg, exc, *, minimum_delay=0):
    """Return updates only for known transient failures, never grant permissions."""
    text = clean(str(exc))
    if any(marker in text.lower() for marker in OPERATOR_MARKERS):
        return None
    rate_limited = minimum_delay > 0 and (
        "límite de peticiones" in text
        or "retry-after" in getattr(exc, "stdout", "").lower()
        or "x-ratelimit-remaining: 0" in getattr(exc, "stdout", "").lower()
    )
    if not retryable(exc) and not rate_limited:
        return None
    conflict = job.get("pr_conflict")
    try:
        history = reserve(
            job,
            cfg,
            "retry",
            stage=job["state"],
            reason=text,
            candidate=job.get("candidate"),
        )
    except RuntimeError:
        return None
    updates = {
        "recoveries": history,
        "recovery_wait": wait(cfg, history, job["state"], minimum_delay),
        "reason": text,
    }
    if job["state"] == "building":
        updates["build_recovery"] = {
            **(job.get("build_recovery") or {"type": "retry"}),
            "started": False,
        }
    if conflict:
        updates["pr_conflict"] = conflict | {"attempt": len(history), "decision": text}
    return updates
