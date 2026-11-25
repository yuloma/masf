"""Observe checks on a PR head and persist decisions for that observation."""

import re
import time

from .queue import api, eligible, fingerprint, retry_delay, save
from .system import ProcessError, clean


PASSING = {"success", "neutral", "skipped"}


def branch(job):
    return f"masf/issue-{job['number']}"


def expected_head(job):
    return job.get("published_head") or job.get("intent") or job.get("candidate")


def pull(job, lookup=api):
    return lookup(f"repos/{job['slug']}/pulls/{job['pr_number']}")


def provenance(job, pr):
    head, base = pr.get("head") or {}, pr.get("base") or {}
    return (
        pr["number"] == job["pr_number"]
        and head.get("ref") == branch(job)
        and (head.get("repo") or {}).get("id") == job["repo_id"]
        and (base.get("repo") or {}).get("id") == job["repo_id"]
        and base.get("ref") == job["branch"]
        and (
            job.get("linked_pr") == pr["number"]
            or (job.get("owner") and f"<!-- masf:{job['owner']} -->" in (pr.get("body") or ""))
        )
    )


def validate(db, job, pr, heads):
    if pr["state"] != "open":
        save(db, job, "merged" if pr.get("merged_at") else "closed", reason="")
    elif not provenance(job, pr):
        reason = "Verifica el repositorio, la rama y la procedencia del PR"
        if job.get("reason") != reason:
            retry = job["state"]
            if retry in {"blocked", "needs_input"}:
                retry = job.get("retry", "pr_open")
            save(db, job, "blocked", reason=reason, retry=retry)
    elif pr["head"]["sha"] not in heads:
        head_conflict(db, job, pr["head"]["sha"])
    else:
        return True
    return False


def head_conflict(db, job, observed, remote=None):
    expected = expected_head(job)
    prior = job.get("head_conflict") or {}
    if (
        job["state"] == "needs_input"
        and prior.get("expected") == expected
        and prior.get("observed") == observed
        and (remote is None or prior.get("remote") == remote)
    ):
        return
    save(
        db, job, "needs_input",
        reason=f"Head inesperado. Usa resume --accept-head {observed} tras revisar los cambios",
        retry="pr_open", head_conflict={"expected": expected, "observed": observed, "remote": remote},
    )


def scope(db, job, lookup=api):
    issue = lookup(f"repos/{job['slug']}/issues/{job['number']}")
    if not eligible(issue, job["repo"]):
        save(db, job, "cancelled", reason="Issue cerrada o etiqueta retirada")
        return False
    observed = fingerprint(issue)
    if observed == job["fingerprint"]:
        return True
    if job["state"] != "needs_input" or job.get("scope_conflict") != observed:
        save(
            db, job, "needs_input", retry="planning", scope_conflict=observed,
            reason="El contenido de la issue ha cambiado. Confirma el alcance actualizado con resume",
        )
    return False


def pages(lookup, endpoint, field=None):
    for page in range(1, 1001):
        separator = "&" if "?" in endpoint else "?"
        data = lookup(f"{endpoint}{separator}per_page=100&page={page}")
        items = data[field] if field else data
        yield from items
        if len(items) < 100:
            return
    raise ValueError("GitHub devolvió una paginación de CI incompleta")


def excerpt(value, limit=2400):
    value = clean(value or "")
    value = re.sub(r"(?i)(\b[\w-]*(?:token|secret|password|signature|api[_-]?key)\s*[:=]\s*)[^\s&]+",
                   r"\1[redacted]", value)
    value = re.sub(r"https?://[^/\s]+:[^/\s]+@", "https://[redacted]@", value)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)[:limit]


def observe(job, sha, lookup=api):
    repo = f"repos/{job['slug']}"
    commit = f"{repo}/commits/{sha}"
    checks = {}
    pending_runs = []
    # Workflow attempts are needed even before a rerun has created check runs.
    runs = pages(lookup, f"{repo}/actions/runs?head_sha={sha}", "workflow_runs")
    for run in runs:
        if run["head_sha"] != sha or run.get("head_branch") != branch(job):
            continue
        run_id, attempt = run["id"], run["run_attempt"]
        if run["status"] != "completed":
            pending_runs.append([run_id, attempt])
            continue
        jobs = pages(lookup, f"{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        conclusions = []
        for item in jobs:
            if item.get("head_sha", sha) != sha:
                continue
            conclusions.append(item.get("conclusion"))
            url = item.get("check_run_url") or ""
            check_id = url.rsplit("/", 1)[-1]
            checks[f"actions:{run_id}:{item['id']}"] = {
                "id": item["id"], "check_id": int(check_id) if check_id.isdecimal() else None,
                "run": run_id, "attempt": attempt, "name": excerpt(item["name"], 200),
                "status": item["status"], "conclusion": item.get("conclusion"),
                "steps": [excerpt(step["name"], 160) for step in item.get("steps", [])
                          if step.get("conclusion") == "failure"][:12],
            }
        if not conclusions or (
            run.get("conclusion") not in PASSING and all(result in PASSING for result in conclusions)
        ):
            checks[f"workflow:{run_id}"] = {
                "id": run_id, "run": run_id, "attempt": attempt, "check_id": None,
                "name": excerpt(run.get("name"), 200), "status": "completed",
                "conclusion": run.get("conclusion"), "steps": [],
            }
    for item in pages(lookup, f"{commit}/check-runs?filter=latest", "check_runs"):
        if item.get("head_sha") != sha or (item.get("app") or {}).get("slug") == "github-actions":
            continue
        checks[f"check:{item['id']}"] = {
            "id": item["id"], "check_id": item["id"], "run": None, "attempt": None,
            "name": excerpt(item["name"], 200), "status": item["status"],
            "conclusion": item.get("conclusion"), "steps": [],
        }
    contexts = set()
    for item in pages(lookup, f"{commit}/statuses"):
        context = item["context"]
        if context in contexts:
            continue
        contexts.add(context)
        checks[f"status:{item['id']}"] = {
            "id": item["id"], "check_id": None, "run": None, "attempt": None,
            "name": excerpt(context, 200),
            "status": "pending" if item["state"] == "pending" else "completed",
            "conclusion": item["state"], "steps": [],
        }
    items = sorted(checks.values(), key=lambda item: (item["name"], item["id"]))
    failures = [item for item in items if item["conclusion"] not in PASSING]
    if pending_runs or any(item["status"] != "completed" for item in items):
        state = "pending"
    elif failures:
        state = "failed"
    elif items:
        state = "success"
    else:
        state = "waiting"
    return dict(sha=sha, state=state, checks=items, pending_runs=sorted(pending_runs)), failures


SIGNALS = {
    "quota": r"rate.limit|quota|too many requests|\b429\b",
    "access": r"permission denied|access denied|unauthorized|bad credentials|\b40[13]\b",
    "configuration": r"invalid workflow|configuration error|workflow syntax|missing required (input|secret)",
    "infrastructure": r"runner lost|runner unavailable|runner.*shut down|connection (reset|timed out)|service unavailable|\b50[234]\b",
    "functional": r"assertionerror|assertion failed|\bfailed test\b|\bFAIL(?:ED)?:?\s+test[_.:]|\btest_\w+.*\b(?:failed|error)\b|\b[1-9]\d* failed\b",
}


def classify(item, evidence):
    if item["conclusion"] in ("timed_out", "startup_failure", "stale"):
        return "infrastructure"
    if item["conclusion"] not in ("failure", "error", "action_required"):
        return "unknown"
    kinds = [kind for kind, pattern in SIGNALS.items() if re.search(pattern, evidence, re.I)]
    return kinds[0] if kinds else "unknown"


def diagnose(job, item, lookup=api):
    endpoint = f"repos/{job['slug']}/check-runs/{item['check_id']}/annotations"
    sources = []
    evidence = []
    if item["check_id"]:
        sources.append(endpoint)
        try:
            for index, annotation in enumerate(pages(lookup, endpoint)):
                if index >= 16:
                    break
                evidence.append(excerpt(" ".join(str(annotation.get(key) or "")
                                                 for key in ("path", "title", "message")), 400))
        except ProcessError as exc:
            if retry_delay():
                raise
            evidence.append(excerpt(str(exc), 250))
    if item["run"] and item["check_id"]:
        endpoint = f"repos/{job['slug']}/actions/jobs/{item['id']}/logs"
        sources.append(endpoint)
        try:
            logs = lookup(endpoint, raw=True)
            patterns = "|".join(SIGNALS.values())
            for line in logs.splitlines():
                if re.search(patterns, line, re.I):
                    evidence.append(excerpt(line, 400))
                if len(evidence) >= 20:
                    break
        except ProcessError as exc:
            if retry_delay():
                raise
            evidence.append(excerpt(str(exc), 250))
    details = excerpt("\n".join([*item["steps"], *evidence]))
    return dict(check=item, evidence=details, sources=sources, kind=classify(item, details))


def limits(job, cfg):
    service = cfg["service"]
    extra = sum(entry["rounds"] for entry in job.get("decisions", [])
                if entry["kind"] == "rounds" and entry["fingerprint"] == job["fingerprint"])
    return dict(rounds=job.get("rounds", 0), functional=job.get("ci_rounds", 0),
                build_limit=service["max_build_rounds"] + extra,
                technical=job.get("ci_technical", 0),
                technical_limit=service.get("max_ci_technical_retries", 2))


def observe_and_route(db, job, cfg, lookup=api, *, revalidate=False):
    sha = expected_head(job)
    observation, failures = observe(job, sha, lookup)
    previous = job.get("ci")
    if observation["state"] != "failed":
        if previous != observation or revalidate or job.get("ci_poll_error"):
            save(db, job, "pr_open", ci=observation, ci_repair=None, ci_retry_at=0,
                 ci_poll_error=None, reason="")
        return False

    key = [[item["run"], item["attempt"], item["id"], item["conclusion"]]
           for item in sorted(failures, key=lambda item: (item["run"] or 0, item["id"]))]
    active = job.get("ci_repair") or {}
    if active.get("sha") == sha and active.get("key") == key:
        if job.get("ci_poll_error"):
            save(db, job, job["state"], ci_poll_error=None, ci_retry_at=0, reason="")
        return True
    if any(entry["sha"] == sha and entry["key"] == key
           for entry in job.get("ci_handled", [])):
        if previous != observation or revalidate:
            save(db, job, "pr_open", ci=observation, ci_repair=None,
                 reason="Fallo ya tramitado. Consulta el historial o solicita una nueva ejecución de CI")
        return False
    if not scope(db, job, lookup):
        return False
    diagnostics = [diagnose(job, item, lookup) for item in failures[:10]]
    if len(failures) > len(diagnostics):
        diagnostics.append({"kind": "unknown", "evidence": "Hay más checks fallidos de los que caben en el diagnóstico"})
    counts = limits(job, cfg)
    repair = dict(sha=sha, key=key, diagnostics=diagnostics, counters=counts,
                  provider=job["repo"]["routing"]["build"], agent="build",
                  model=cfg.get("providers", {}).get(job["repo"]["routing"]["build"], {}).get("model"))
    kinds = {item["kind"] for item in diagnostics}
    fields = dict(ci=observation, ci_repair=repair, ci_poll_error=None,
                  ci_handled=[*job.get("ci_handled", []), {"sha": sha, "key": key}])
    if kinds == {"functional"} and job.get("plan") and counts["rounds"] < counts["build_limit"]:
        save(db, job, "building", **fields, candidate=sha, build_workspace=None, reason="", ci_retry_at=0,
             feedback={"source": "CI", "sha": sha, "diagnostics": diagnostics})
        return True
    if kinds == {"infrastructure"} and counts["technical"] < counts["technical_limit"]:
        delay = cfg["service"].get("ci_retry_seconds", 60)
        save(db, job, "checking", **fields, candidate=sha, checks={}, review={},
             ci_technical=counts["technical"] + 1, ci_retry_at=time.time() + delay,
             reason="Fallo de infraestructura de CI. Verificación local programada")
        return True
    if kinds == {"functional"}:
        reason = (f"Rondas agotadas ({counts['rounds']}/{counts['build_limit']}). Usa resume --add-rounds N"
                  if job.get("plan") else "Confirma el alcance de la issue con resume para generar un plan")
    elif kinds == {"infrastructure"}:
        reason = f"Reintentos técnicos agotados ({counts['technical']}/{counts['technical_limit']}). Corrige el entorno y relanza CI"
    elif "quota" in kinds:
        reason = "Cuota de CI agotada. Restablece la cuota y relanza CI"
    elif "access" in kinds:
        reason = "Acceso a CI insuficiente. Revisa permisos y credenciales y relanza CI"
    elif "configuration" in kinds:
        reason = "Configuración de CI inválida. Corrige el workflow o sus parámetros y relanza CI"
    else:
        reason = "Diagnóstico de CI insuficiente. Revisa los logs del check indicado y relanza CI con evidencia"
    needs_plan = kinds == {"functional"} and not job.get("plan")
    save(db, job, "blocked" if kinds & {"access", "quota", "configuration"} else "needs_input",
         **fields, reason=reason, retry="planning" if needs_plan else "pr_open",
         ci_retry_at=0, ci_intervention=True)
    return False


def poll_failed(db, job, cfg, error):
    service = cfg["service"]
    used = job.get("ci_technical", 0)
    technical_limit = service.get("max_ci_technical_retries", 2)
    delay = max(retry_delay(), service.get("ci_retry_seconds", 60))
    access = re.search(SIGNALS["access"], error, re.I) and not retry_delay()
    if access or used >= technical_limit:
        save(
            db, job, "blocked", ci_poll_error=error, retry=job["state"],
            reason="Sondeo de CI bloqueado. Revisa el acceso y la respuesta de GitHub y usa resume",
        )
    else:
        save(
            db, job, job["state"], ci_poll_error=error, ci_technical=used + 1,
            ci_retry_at=time.time() + delay,
            reason="Sondeo de CI pendiente. Reintento técnico programado",
        )
