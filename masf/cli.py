import argparse
import getpass
from datetime import datetime, timezone
import fcntl
import json
import re
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from . import ci, integration, recovery
from .actions import ActionError, Decisions, process_pending, recorded_question
from .config import ConfigError, load, xdg
from .evidence import UNKNOWN_PROVENANCE, historical_role, provenance
from .observation_api import DEFAULT_PORT, port, serve as serve_observation
from .observe import link_pull, load_row, observe
from .queue import (
    api,
    claim,
    connect,
    eligible,
    fingerprint,
    leased_job,
    lease_active,
    renew,
    latest,
    retry_delay,
    save,
    save_link,
    scan,
    stage_attempts,
)
from .system import TaskStopped, clean, command, docker_env, filtered
from .worker import ProtectedPaths, Worker


RUNNABLE = ("queued", "planning", "building", "checking", "reviewing", "publishing", "retry_wait",
            integration.STATE)


def doctor(cfg, offline):
    for name, provider in cfg["providers"].items():
        print(name, "habilitado" if provider["enabled"] else "deshabilitado")
    if offline:
        return
    print(
        "Docker",
        command(
            ["docker", "version", "--format", "{{.Server.Version}}"], env=docker_env()
        ),
    )
    print(command(["docker", "compose", "version"], env=docker_env()))
    for repo in cfg["repositories"]:
        if repo["enabled"]:
            api(f"repos/{repo['slug']}/issues?per_page=1")
            observed = False
            database = xdg("STATE", ".local/state") / "queue.sqlite3"
            if database.is_file():
                with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
                    observed = (
                        db.execute(
                            "SELECT 1 FROM jobs WHERE slug=? AND state IN ('pr_open','merged','closed') LIMIT 1",
                            (repo["slug"],),
                        ).fetchone()
                        is not None
                    )
            print(
                repo["slug"],
                "lectura verificada;",
                "push y creación de PR verificados en el historial"
                if observed
                else "push y creación de PR pendientes de prueba",
            )
            for name in sorted(set(repo["routing"].values())):
                print(
                    command(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--network",
                            "none",
                            repo["runner_image"],
                            cfg["providers"][name]["executable"],
                            "--version",
                        ],
                        env=docker_env(),
                    )
                )


TERMINAL = {"closed", "merged", "cancelled"}


def process_task(cfg, config, root, job):
    if job.get("state") == "retry_wait" and job["recovery_wait"]["until"] > time.time():
        return 0
    token = uuid.uuid4().hex
    ttl = 90
    stop = threading.Event()
    db = connect(root)
    if not claim(db, job, token, ttl, cfg["service"]["max_parallel_jobs"]):
        db.close()
        return 0

    def heartbeat():
        conn = connect(root)
        try:
            while not stop.wait(ttl / 3):
                if not renew(conn, job, token, ttl):
                    return
        finally:
            conn.close()

    thread = None
    try:
        current = load_row(db.execute(
            "SELECT data FROM jobs WHERE repo_id=? AND number=?",
            (job["repo_id"], job["number"]),
        ).fetchone())
        if current is None or current["state"] not in RUNNABLE or current.get("ci_retry_at", 0) > time.time():
            return 0
        job = current | {"attempt": token}
        if job["state"] == "retry_wait":
            if job["recovery_wait"]["until"] > time.time():
                return 0
            save(db, job, job["recovery_wait"]["stage"], reason="", recovery_wait={})
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        jobs = Path(
            os.environ.get("MASF_JOBS_DIR") or xdg("DATA", ".local/share") / "jobs"
        )
        Worker(db, cfg, config, root, jobs, job).process()
        return 0
    except TaskStopped:
        return 0
    except Exception as exc:
        reason = clean(str(exc))
        try:
            failures = [stage for stage in stage_attempts(db, job)
                        if stage["lease"] == token and stage["status"] == "failed"]
            failed_attempt = failures[-1]["id"] if failures else None
            delay = retry_delay()
            updates = recovery.schedule(job, cfg, exc, minimum_delay=delay)
            if updates is not None:
                save(db, job, "retry_wait", **updates, failed_attempt=failed_attempt)
            else:
                exhausted = None
                if isinstance(exc, recovery.BudgetExhausted):
                    exhausted = "reserve"
                elif (recovery.can_schedule(exc, delay)
                      and len(job.get("recoveries", [])) >= recovery.limit(job, cfg)):
                    exhausted = "retry"
                if exhausted:
                    reason += recovery.EXHAUSTION_NOTE
                details = {"blocked_paths": exc.paths} if isinstance(exc, ProtectedPaths) else {}
                save(db, job, "blocked", retry=job["state"], reason=reason,
                     failed_attempt=failed_attempt, recovery_exhausted=exhausted, **details)
        except RuntimeError:
            if not lease_active(db, job):
                return 0
            raise
        print(reason, file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        with db:
            db.execute("DELETE FROM leases WHERE token=?", (token,))
        db.close()


def poll_ci_jobs(db, cfg):
    if retry_delay():
        return 1
    errors = 0
    active = {repo["slug"] for repo in cfg["repositories"] if repo["enabled"]}
    for row in db.execute(
        """SELECT data FROM jobs WHERE json_extract(data, '$.pr_number') IS NOT NULL
        AND (state NOT IN ('closed','merged','cancelled')
             OR (state='cancelled' AND json_type(data, '$.merge_intent')='object'))"""
    ).fetchall():
        job = json.loads(row[0])
        if (job["slug"] not in active and not job.get("merge_intent")) or not job.get("pr_number"):
            continue
        token = uuid.uuid4().hex
        if not claim(db, job, token, 300):
            continue

        def lookup(endpoint, **kwargs):
            if not renew(db, job, token, 300):
                raise TaskStopped("La reserva del sondeo ha vencido")
            return api(endpoint, **kwargs)

        observing_pr = True
        try:
            job = leased_job(db, job, token)
            if job["state"] in TERMINAL and not job.get("merge_intent"):
                continue
            if job["state"] == "pr_open" and job.get("ci_retry_at", 0) > time.time():
                continue
            pr = ci.pull(job, lookup)
            if integration.confirm(db, job, pr):
                continue
            if job["state"] in TERMINAL:
                continue
            if pr["state"] != "open":
                ci.validate(db, job, pr, set())
            elif not job.get("repair_intent") and ci.scope(db, job, lookup):
                if not ci.validate(db, job, pr, {ci.expected_head(job)}):
                    continue
                repo = integration.registry_for(cfg, job)
                intervention = job.get("merge_intervention") or {}
                prior = (job.get("integration_approval") or intervention).get("binding")
                if prior and prior != integration.binding_for(job, pr, repo):
                    save(db, job, "pr_open", integration_approval=None, merge_intervention=None,
                         merge_evaluation=None, merge_waits=0, ci_retry_at=0,
                         reason="El contexto de integración cambió")
                if job["state"] == "pr_open":
                    recovering = ci.observe_merge(db, job, pr, lookup)
                    observing_pr = False
                    if not recovering:
                        previous = job.get("ci")
                        ci.observe_and_route(db, job, cfg, lookup)
                        integration.schedule(db, job, pr, repo, previous)
                elif job.get("ci_intervention") and not job.get("head_conflict"):
                    observation, _ = ci.observe(job, ci.expected_head(job), lookup)
                    observation = filtered(observation)
                    if observation != job.get("ci"):
                        save(db, job, job["state"], ci=observation)
        except TaskStopped:
            continue
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            reason = clean(str(exc))
            if (job.get("state") == "pr_open" and job.get("ci_retry_at", 0) <= time.time()
                    and lease_active(db, job)):
                if observing_pr:
                    ci.merge_poll_failed(db, job, cfg, exc)
                else:
                    ci.poll_failed(db, job, cfg, reason)
            print(f"{job['slug']}: {reason}", file=sys.stderr, flush=True)
            errors += 1
        finally:
            with db:
                db.execute("DELETE FROM leases WHERE token=?", (token,))
        if retry_delay():
            break
    return errors


def pending_jobs(db, cfg, running, limit):
    active = {repo["slug"] for repo in cfg["repositories"] if repo["enabled"]}
    if limit <= 0 or retry_delay():
        return
    states = ",".join("?" for _ in RUNNABLE)
    for row in db.execute(
        f"""SELECT jobs.data FROM jobs LEFT JOIN leases USING(repo_id,number)
        WHERE jobs.state IN ({states})
        AND (leases.expires IS NULL OR leases.expires<=unixepoch()) ORDER BY jobs.rowid""",
        RUNNABLE,
    ):
        job = load_row(row)
        key = job["repo_id"], job["number"]
        if (job["slug"] in active and key not in running
                and job.get("ci_retry_at", 0) <= time.time()
                and (job["state"] != "retry_wait" or job["recovery_wait"]["until"] <= time.time())):
            yield key, job
            limit -= 1
            if limit == 0:
                break


class Workers:
    def __init__(self):
        self.running = {}
        self.pool = None
        self.capacity = 0

    def dispatch(self, db, cfg, config, root, *, once=False):
        errors = self.reap()
        limit = 1 if once else cfg["service"]["max_parallel_jobs"]
        if limit > self.capacity:
            self.close(wait_for_jobs=False)
            self.pool = ThreadPoolExecutor(limit)
            self.capacity = limit
        for key, job in pending_jobs(db, cfg, self.running, limit - len(self.running)):
            self.running[key] = self.pool.submit(process_task, cfg, config, root, job)
        return errors + (self.reap(wait=True) if once else 0)

    def reap(self, wait=False):
        errors = 0
        for key, future in tuple(self.running.items()):
            if wait or future.done():
                del self.running[key]
                errors += future.result()
        return errors

    def close(self, wait_for_jobs=True):
        if wait_for_jobs:
            wait(self.running.values())
        if self.pool is not None:
            self.pool.shutdown(wait=wait_for_jobs)


def tick(db, cfg, config, root, once, workers=None):
    errors = process_pending(db, cfg, root, lookup=api, latest=latest)
    repository_ids = {}
    errors += scan(cfg, db, repository_ids=repository_ids) + observe(db, api, cfg, repository_ids)
    errors += poll_ci_jobs(db, cfg)
    if workers is not None:
        return errors + workers.dispatch(db, cfg, config, root, once=once)
    workers = Workers()
    try:
        return errors + workers.dispatch(db, cfg, config, root, once=once) + workers.reap(wait=True)
    finally:
        workers.close()


def decision_target(db, cfg, target):
    slug, number = target.rsplit("#", 1)
    row = db.execute("SELECT repo_id,number FROM jobs WHERE slug=? AND number=?",
                     (slug, int(number))).fetchone()
    if not row:
        raise ValueError("Trabajo desconocido")
    root = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
    return Decisions(root, cfg, operator_name(), "cli", lookup=api, latest=latest), tuple(row)


def resume(db, cfg, target, allow_protected_paths=(), reuse_workspace=None, resolved_commit=None, *,
           accept_head=None, add_rounds=None, evidence=None, revision=None, idempotency_key=None,
           answer=False, confirm_scope=False, role_provider=None, timeout=None, workspace_reference=None,
           add_recoveries=None, retry_merge_review=False, record_manual_merge=False):
    operation, identity = decision_target(db, cfg, target)
    job, current = operation.read_job(db, identity)
    explicit = any(value is not None for value in (revision, idempotency_key))
    if explicit and not (revision and idempotency_key and evidence):
        raise ValueError("Indica --revision, --idempotency-key y --evidence juntos")
    if timeout is not None and not workspace_reference:
        raise ValueError("--timeout-seconds requiere --workspace-reference")
    if reuse_workspace is not None and workspace_reference:
        raise ValueError("Selecciona una sola referencia de copia")
    choices = []
    if allow_protected_paths:
        choices.append(("allow_paths", {"paths": list(allow_protected_paths)}))
    if workspace_reference:
        if timeout is None:
            choices.append(("reuse_workspace", {"reference": workspace_reference}))
        else:
            choices.append(("extend_timeout", {"seconds": timeout, "reference": workspace_reference}))
    if reuse_workspace is not None:
        choices.append(("reuse_workspace", {"path": str(Path(reuse_workspace).absolute())}))
    if resolved_commit is not None:
        choices.append(("resolved_commit", {"sha": resolved_commit}))
    if accept_head is not None:
        choices.append(("accept_head", {"sha": accept_head} | (
            {"extra": add_rounds} if add_rounds is not None else {})))
    elif add_rounds is not None:
        choices.append(("add_rounds", {"extra": add_rounds}))
    if add_recoveries is not None:
        choices.append(("add_recoveries", {"extra": add_recoveries}))
    if role_provider is not None:
        role, provider = role_provider
        choices.append(("assign_provider", {"role": role, "provider": provider}))
    if answer:
        choices.append(("answer", {}))
    if confirm_scope:
        choices.append(("confirm_scope", {}))
    if retry_merge_review:
        choices.append(("retry_merge_review", {}))
    if record_manual_merge:
        if not evidence:
            raise ValueError("Indica el motivo de la decisión mediante --evidence")
        choices.append(("record_manual_merge", {}))
    if len(choices) > 1:
        raise ValueError("Selecciona una sola decisión")
    kind, options = choices[0] if choices else ("resolve", {})
    if not choices and not explicit:
        if job.get("scope_conflict"):
            kind = "confirm_scope"
        elif job["state"] == "needs_input" and recorded_question(db, job):
            kind = "answer"
    if kind == "answer" and not evidence:
        raise ValueError("Escribe la respuesta mediante --evidence")
    return operation.submit(db, identity, idempotency_key or uuid.uuid4().hex, {
        "kind": kind, "revision": revision or current, "confirm": True, "options": options,
        "evidence": evidence or job.get("reason") or "Reanudación autorizada desde la CLI",
    })


def operator_name():
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return f"uid:{os.getuid()}"


def decision(job, kind, evidence, **details):
    if not evidence or not evidence.strip():
        raise ValueError("Indica la evidencia de la decisión mediante --evidence")
    return dict(kind=kind, operator=operator_name(), fingerprint=job["fingerprint"],
                at=datetime.now(timezone.utc).isoformat(), evidence=ci.excerpt(evidence),
                **details)


def approved_head(job, sha):
    pr = ci.pull(job, api)
    if (pr["state"] != "open" or not ci.provenance(job, pr)
            or pr["head"]["sha"] != sha):
        raise ValueError("El PR abierto debe coincidir con la procedencia y el SHA autorizados")
    ref = api(f"repos/{job['slug']}/git/ref/heads/{ci.branch(job)}")
    if ref["object"]["sha"] != sha:
        raise ValueError("La referencia remota debe coincidir con el SHA autorizado")
    return pr


def link_pr(db, cfg, target, pr_number, sha, evidence):
    slug, number = target.rsplit("#", 1)
    number = int(number)
    repo = next((item for item in cfg["repositories"] if item["enabled"] and item["slug"] == slug), None)
    if not repo or pr_number <= 0 or number <= 0:
        raise ValueError("Indica un repositorio habilitado y números válidos de issue y PR")
    meta = api(f"repos/{slug}")
    issue = api(f"repos/{slug}/issues/{number}")
    if not eligible(issue, repo):
        raise ValueError("La issue debe estar abierta y etiquetada para MASF")
    base_branch = repo.get("base_branch") or meta["default_branch"]
    job = dict(repo_id=meta["id"], number=number, slug=slug, repo=repo, branch=base_branch,
               fingerprint=fingerprint(issue), title=issue["title"], body=issue["body"],
               rounds=0, state="queued", pr_number=pr_number, linked_pr=pr_number)
    pr = approved_head(job, sha)
    base = api(f"repos/{slug}/commits/{base_branch}")["sha"]
    entry = decision(job, "link", evidence, pr=pr_number, head=sha, base=base)
    token = uuid.uuid4().hex
    if not claim(db, job, token, 300):
        raise ValueError("El trabajo tiene un intento activo")
    try:
        approved_head(job, sha)
        current_issue = api(f"repos/{slug}/issues/{number}")
        if not eligible(current_issue, repo) or fingerprint(current_issue) != job["fingerprint"]:
            raise ValueError("La elegibilidad o el alcance han cambiado durante la vinculación")
        job["provenance"] = provenance(cfg, job)
        job.update(attempt=token, state="pr_open", base=base, pr=pr["html_url"],
                   published_head=sha, candidate=sha, decisions=[entry],
                   ci={"sha": sha, "state": "waiting", "checks": [], "pending_runs": []})
        save_link(db, job, entry)
    finally:
        with db:
            db.execute("DELETE FROM leases WHERE token=?", (token,))


def pull_target(value):
    slug, sep, number = value.rpartition("#")
    if not sep or slug.count("/") != 1 or not number.isascii() or not number.isdecimal() or int(number) < 1:
        raise argparse.ArgumentTypeError("Usa propietario/repositorio#número")
    return slug, int(number)


def pull_output(db, target, *, history=False, sha=None):
    def cli_snapshot(row):
        snapshot = load_row(row)
        if snapshot is not None:
            snapshot.pop("observed_at", None)
            snapshot.pop("last_success_at", None)
        return filtered(snapshot)

    params = target or ()
    pull_filter = " WHERE slug=? COLLATE NOCASE AND number=?" if target else ""
    for row in db.execute("SELECT data FROM pull_requests" + pull_filter + " ORDER BY slug,number", params):
        pr = filtered(load_row(row))
        ident = pr["repo_id"], pr["number"]
        label = {"repo_id": pr["repo_id"], "number": pr["number"]}
        if not history:
            ci = cli_snapshot(db.execute(
                "SELECT data FROM pull_ci WHERE repo_id=? AND number=? AND sha=?",
                (*ident, pr["head"]["sha"]),
            ).fetchone())
            print(json.dumps({"type": "pull_request", **pr, "ci": ci}))
            continue
        sha_filter = " AND sha=? COLLATE NOCASE" if sha else ""
        params_with_sha = (*ident, sha) if sha else ident
        for item in db.execute(
            "SELECT id,at,sha,kind,detail FROM pull_events WHERE repo_id=? AND number=?"
            + sha_filter + " ORDER BY id", params_with_sha,
        ):
            print(json.dumps({"type": "pull_event", **label, **dict(item), "detail": filtered(json.loads(item["detail"]))}))
        for item in db.execute(
            "SELECT sha,data FROM pull_ci WHERE repo_id=? AND number=?" + sha_filter + " ORDER BY sha",
            params_with_sha,
        ):
            print(json.dumps({"type": "pull_evidence", **label, "sha": item["sha"], "ci": cli_snapshot(item)}))

    if sha:
        return
    source_filter = " WHERE slug=? COLLATE NOCASE" if target else ""
    slug_params = (target[0],) if target else ()
    if history:
        for row in db.execute("SELECT * FROM pull_source_events" + source_filter + " ORDER BY id", slug_params):
            if target and row["source"].startswith("pull:") and row["source"] != f"pull:{target[1]}":
                continue
            print(json.dumps({
                "type": "pull_source_event", **dict(row),
                "error": json.loads(row["error"]) if row["error"] else None,
            }))
    else:
        for row in db.execute("SELECT * FROM pull_sources" + source_filter + " ORDER BY slug", slug_params):
            errors = json.loads(row["error"])
            if target:
                errors = {key: value for key, value in errors.items()
                          if not key.startswith("pull:") or key == f"pull:{target[1]}"}
            if errors:
                print(json.dumps({
                    "type": "pull_source", "slug": row["slug"],
                    "changed_at": row["changed_at"], "errors": errors,
                }))


def job_evidence(db, job, root):
    records = stage_attempts(db, job)
    for role in ("plan", "build", "review"):
        record = job.get(role) or {}
        if record and not (record.get("stage_attempt") or job.get(role + "_attempt")):
            records.append(historical_role(role, record))
    folder = root / "evidence" / f"{job['repo_id']}-{job['number']}"
    known_files = {record.get("evidence_file") for record in records}
    for role in ("plan", "build", "review"):
        for file in sorted(folder.glob(f"{role}-*.json")):
            if file.name in known_files:
                continue
            try:
                record = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and "id" not in record and (record.get("error") or record.get("result")):
                records.append(historical_role(role, record | {"evidence_file": file.name}))
    return {"provenance": job.get("provenance") or UNKNOWN_PROVENANCE,
            "stage_attempts": records}


def main():
    parser = argparse.ArgumentParser(prog="masf")
    parser.add_argument("--config", type=Path)
    subs = parser.add_subparsers(dest="cmd", required=True)
    subs.add_parser("doctor").add_argument("--offline", action="store_true")
    subs.add_parser("scan").add_argument("--dry-run", action="store_true")
    subs.add_parser("run").add_argument("--once", action="store_true", required=True)
    resume_parser = subs.add_parser("resume")
    resume_parser.add_argument("issue")
    resume_parser.add_argument(
        "--allow-protected-path",
        action="append",
        default=[],
        help="Autoriza una ruta exacta para este trabajo y su alcance vigente",
    )
    resume_parser.add_argument(
        "--reuse-workspace",
        type=Path,
        help="Recupera una copia conservada de este trabajo tras una interrupción",
    )
    resume_parser.add_argument(
        "--resolved-commit",
        help="Selecciona un commit local que resuelve la actualización de base",
    )
    decisions = resume_parser.add_argument_group("decisiones del operador")
    decisions.add_argument("--decision-context", action="store_true", help="Muestra la revisión y opciones vigentes")
    decisions.add_argument("--revision", help="Revisión esperada del trabajo")
    decisions.add_argument("--idempotency-key", help="Clave estable de 16 a 128 caracteres")
    decisions.add_argument("--answer", action="store_true", help="Usa --evidence como respuesta a la pregunta")
    decisions.add_argument("--confirm-scope", action="store_true", help="Confirma el alcance observado")
    decisions.add_argument("--assign-provider", nargs=2, metavar=("ROL", "PROVEEDOR"))
    decisions.add_argument("--timeout-seconds", type=int)
    decisions.add_argument("--workspace-reference")
    decisions.add_argument("--add-recoveries", type=int)
    decisions.add_argument("--accept-head")
    decisions.add_argument("--add-rounds", type=int)
    decisions.add_argument("--retry-merge-review", action="store_true")
    decisions.add_argument("--record-manual-merge", action="store_true")
    decisions.add_argument("--evidence")
    link = subs.add_parser("link-pr")
    link.add_argument("target", type=pull_target)
    link.add_argument("linked_issue", type=pull_target, nargs="?")
    link.add_argument("--pr", type=int)
    link.add_argument("--head")
    link.add_argument("--evidence")
    status = subs.add_parser("status")
    status.add_argument("--json", action="store_true")
    history = subs.add_parser("history")
    history.add_argument("issue")
    status.add_argument("--pr", type=pull_target)
    logs = subs.add_parser("logs")
    logs.add_argument("--pr", type=pull_target)
    logs.add_argument("--sha")
    subs.add_parser("serve")
    observation = subs.add_parser("observe-api", help="Sirve la API local de solo lectura")
    observation.add_argument("--port", type=port, default=DEFAULT_PORT, help="Puerto TCP (por defecto %(default)s)")
    observation.add_argument("--actions-token-file", type=Path, help="Archivo privado de credenciales del operador")
    args = parser.parse_args()
    config = (args.config or xdg("CONFIG", ".config") / "config.toml").absolute()
    db = None
    try:
        if args.cmd == "observe-api":
            serve_observation(xdg("STATE", ".local/state"), config, args.port, args.actions_token_file)
            return 0
        cfg = load(config)
        if args.cmd == "doctor":
            doctor(cfg, args.offline)
            return 0
        if args.cmd == "scan" and args.dry_run:
            return bool(scan(cfg))
        root = xdg("STATE", ".local/state")
        db = connect(root)
        if args.cmd == "scan":
            return bool(scan(cfg, db))
        if args.cmd == "status":
            if not args.pr:
                for row in db.execute("SELECT data FROM jobs ORDER BY rowid"):
                    j = json.loads(row[0])
                    observed = j.get("ci") or {}
                    if args.json:
                        print(json.dumps(filtered(j | job_evidence(db, j, root) |
                                                  {"limits": ci.limits(j, cfg),
                                                   "conflict_action": ci.conflict_action(j)}),
                                         ensure_ascii=False))
                        continue
                    counts = ci.limits(j, cfg)
                    conflict = j.get("pr_conflict") or j.get("merge_observation") or {}
                    next_action = ci.conflict_action(j)
                    print(
                        f"{j['slug']}#{j['number']} {j['state']}",
                        j.get("candidate", ""),
                        j.get("pr", ""),
                        f"CI {observed.get('state', '—')} {observed.get('sha', '')}",
                        f"rondas {counts['rounds']}/{counts['build_limit']}",
                        f"reintentos técnicos {counts['technical']}/{counts['technical_limit']}",
                        (f"merge {conflict['status']} base={conflict.get('base') or '—'} "
                         f"head={conflict.get('head') or '—'} "
                         f"{', '.join(conflict.get('paths', []))}") if conflict else "",
                        j.get("reason", ""),
                        next_action if next_action and next_action != j.get("reason") else "",
                        j.get("ci_poll_error", ""),
                        ("reintento=" + datetime.fromtimestamp(
                            j["recovery_wait"]["until"], timezone.utc
                        ).isoformat()) if j["state"] == "retry_wait" else "",
                    )
                    if request := j.get("merge_intervention"):
                        binding = request.get("binding") or {}
                        evaluation = j.get("merge_evaluation") or {}
                        if evaluation.get("binding") != binding:
                            evaluation = {}
                        print(f"  PR {binding.get('pr', j.get('pr_number'))} "
                              f"base={binding.get('base', '—')} head={binding.get('head', '—')}")
                        print("  Evidencia:", ", ".join([*evaluation.get("ci", {}), *evaluation.get("local", {})]) or "pendiente")
                        for reason in request["reasons"]:
                            print("  " + reason)
                        print("  " + request["next_action"])
            if args.pr or not args.json:
                pull_output(db, args.pr)
            return 0
        if args.cmd == "logs":
            pull_output(db, args.pr, history=True, sha=args.sha)
            return 0
        if args.cmd == "history":
            slug, number = args.issue.rsplit("#", 1)
            for row in db.execute(
                """SELECT e.* FROM events e JOIN jobs j USING(repo_id, number)
                WHERE j.slug=? AND j.number=? ORDER BY e.id""", (slug, int(number))
            ):
                detail = json.loads(row["detail"])
                for role in ("plan", "build", "review"):
                    record = detail.get(role)
                    if isinstance(record, dict) and record and not (
                        record.get("stage_attempt") or detail.get(role + "_attempt")
                    ):
                        detail.setdefault("historical_stages", []).append(historical_role(role, record))
                print(json.dumps(filtered(dict(row) | {"detail": detail}), ensure_ascii=False))
            return 0
        if args.cmd == "resume":
            if args.decision_context:
                operation, identity = decision_target(db, cfg, args.issue)
                result = operation.context(db, identity)
            else:
                result = resume(
                    db, cfg, args.issue, args.allow_protected_path, args.reuse_workspace,
                    resolved_commit=args.resolved_commit, accept_head=args.accept_head,
                    add_rounds=args.add_rounds, evidence=args.evidence, revision=args.revision,
                    idempotency_key=args.idempotency_key, answer=args.answer,
                    confirm_scope=args.confirm_scope, role_provider=args.assign_provider,
                    timeout=args.timeout_seconds, workspace_reference=args.workspace_reference,
                    add_recoveries=args.add_recoveries,
                    retry_merge_review=args.retry_merge_review,
                    record_manual_merge=args.record_manual_merge,
                )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.cmd == "serve":
            print(
                f"MASF serve activo, configuración {config}, sondeo cada {cfg['service']['poll_seconds']}s",
                flush=True,
            )
        workers = Workers()
        try:
            with (root / "worker.lock").open("a") as lock:
                while True:
                    started = time.monotonic()
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        if args.cmd == "link-pr":
                            if args.linked_issue is not None:
                                if args.pr is not None or args.head or args.evidence:
                                    raise ValueError("La vinculación de observación usa solo PR e issue")
                                link_pull(db, args.target, args.linked_issue, operator=operator_name())
                            else:
                                if args.pr is None or not args.head or not args.evidence:
                                    raise ValueError("La vinculación para reparar requiere --pr, --head y --evidence")
                                slug, number = args.target
                                link_pr(db, cfg, f"{slug}#{number}", args.pr, args.head, args.evidence)
                            return 0
                        cfg = load(config)
                        errors = tick(db, cfg, config, root, args.cmd == "run", workers)
                    except Exception as exc:
                        if args.cmd != "serve":
                            raise
                        print(clean(str(exc)), file=sys.stderr, flush=True)
                        errors = 1
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    if args.cmd == "run":
                        return bool(errors)
                    delay = max(retry_delay(), started + cfg["service"]["poll_seconds"] - time.monotonic())
                    print(
                        f"MASF sondeo completado, errores={errors}, espera={delay:.0f}s",
                        flush=True,
                    )
                    time.sleep(delay)
        finally:
            workers.close()
    except ActionError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(clean(f"{exc} ({config})"), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(clean(str(exc)), file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
