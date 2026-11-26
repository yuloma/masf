"""Provenance and the versioned operator-facing attempt format."""

import hashlib
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from .system import filtered


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_artifact(path, value):
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(filtered(value), stream, indent=2)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@lru_cache
def revisions():
    sources = {path.name: path.read_text() for path in Path(__file__).parent.glob("*.py")}
    policy = ("actions.py", "checks.py", "ci.py", "cli.py", "config.py", "github_ci.py", "integration.py", "observe.py",
              "providers.py", "queue.py", "recovery.py", "worker.py")
    return {"engine": "sha256:" + digest(sources),
            "policy": "sha256:" + digest({name: sources[name] for name in policy})}


UNKNOWN_PROVENANCE = {"engine": None, "policy": None, "configuration": None}


def provenance(cfg, job):
    repo = job["repo"]
    config = {"version": cfg.get("version"), "service": cfg.get("service", {}),
              "repository": repo | {"base_branch": job.get("branch")},
              "providers": {name: cfg.get("providers", {}).get(name, {})
                            for name in sorted(set(repo.get("routing", {}).values()))}}
    return revisions() | {"configuration": filtered(config)}


def model(requested=None, *, applicable=True):
    return {"requested": requested, "effective": None, "source": None,
            "availability": "unknown" if applicable else "not_applicable"}


def usage(value=None):
    return {"availability": "unknown" if value is None else "reported", "measurements": value or []}


def reported_usage(event, source):
    measurements = []
    values = event.get("usage")
    if isinstance(values, dict):
        for name, value in values.items():
            if type(value) in (int, float) and (name == "tokens" or name.endswith(("_tokens", "Tokens"))):
                measurements.append({"name": name, "value": value, "unit": "tokens",
                                     "source": f"{source}:usage.{name}"})
    for field, kind in (("total_cost_usd", "estimated"), ("billed_cost_usd", "confirmed")):
        if type(event.get(field)) in (int, float):
            measurements.append({"name": "cost", "value": event[field], "unit": "USD",
                                 "kind": kind, "source": f"{source}:{field}"})
    return measurements


def explanation(summary=None, *, decisions=(), criteria=(), evidence=(), reason=None,
                source="controller"):
    return {"availability": "unknown" if summary is None else "available",
            "summary": summary, "decisions": list(decisions), "criteria": list(criteria),
            "evidence": [ref for ref in evidence if ref], "reason": reason,
            "source": source if summary is not None else None}


def agent_explanation(role, result, refs, plan=None):
    summary = result.get("summary")
    criteria = result.get("criteria", []) if role == "plan" else (plan or {}).get("criteria", [])
    decisions = result.get("changes", []) if role == "build" else result.get("findings", [])
    verdict = result.get("verdict")
    if role == "merge_review" and result.get("decision"):
        decisions = [result["decision"], *result.get("reasons", [])]
    if verdict:
        decisions = [verdict, *decisions]
        summary = summary or ("Revisión aprobada" if verdict == "pass" else "Revisión con cambios solicitados")
    reason = result.get("question")
    if role == "plan" and not summary:
        summary = reason or ("Criterios de aceptación registrados" if criteria else None)
    if role == "build" and not summary and decisions:
        summary = "Cambios comunicados por la etapa"
    if verdict == "changes_requested":
        reason = "; ".join(result.get("findings", [])) or None
    return explanation(summary, decisions=decisions, criteria=criteria, evidence=refs,
                       reason=reason, source="stage_result")


def historical_role(role, record):
    result = record.get("result") or (record if "summary" in record else {})
    identity = model()
    # The old model field could contain either a CLI report or a configured alias.
    return {"version": 1, "historical": True, "id": record.get("stage_attempt"),
            "role": role, "provider": record.get("provider"), "execution_id": None,
            "executor": record.get("provider"),
            "lease": record.get("attempt"), "input_sha": record.get("sha"),
            "candidate_sha": None, "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
            "duration_seconds": record.get("seconds"), "cli_version": record.get("version")
            if isinstance(record.get("version"), str) else None,
            "model": identity, "legacy_model": record.get("model"),
            "usage": usage(reported_usage(record, "historical") or None), "legacy_usage": record.get("usage"),
            "status": "failed" if record.get("error") else "unknown" if not result else "completed",
            "provenance": UNKNOWN_PROVENANCE, "result": result or None, "error": record.get("error"),
            "explanation": (explanation(record["error"], reason=record["error"],
                                        evidence=[record.get("evidence_file")], source="historical")
                            if record.get("error") else agent_explanation(
                                role, result, [record.get("evidence_file")]))}
