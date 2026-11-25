import argparse
import fnmatch
import getpass
from datetime import datetime, timezone
import fcntl
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path, PurePosixPath

from . import ci
from .config import ConfigError, load, xdg
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
    retry_delay,
    save,
    save_link,
    scan,
)
from .system import TaskStopped, clean, command, docker_env
from .worker import Worker, saved_workspace


RUNNABLE = ("queued", "planning", "building", "checking", "reviewing", "publishing")


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
            save(db, job, "blocked", retry=job["state"], reason=reason)
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
        """SELECT data FROM jobs WHERE state NOT IN ('closed','merged','cancelled')
        AND json_extract(data, '$.pr_number') IS NOT NULL"""
    ).fetchall():
        job = json.loads(row[0])
        if job["slug"] not in active or not job.get("pr_number"):
            continue
        token = uuid.uuid4().hex
        if not claim(db, job, token, 300):
            continue

        def lookup(endpoint, **kwargs):
            if not renew(db, job, token, 300):
                raise TaskStopped("La reserva del sondeo ha vencido")
            return api(endpoint, **kwargs)

        try:
            job = leased_job(db, job, token)
            if job["state"] in TERMINAL:
                continue
            if job["state"] == "pr_open" and job.get("ci_retry_at", 0) > time.time():
                continue
            pr = ci.pull(job, lookup)
            if pr["state"] != "open":
                ci.validate(db, job, pr, set())
            elif not job.get("repair_intent") and ci.scope(db, job, lookup):
                if not ci.validate(db, job, pr, {ci.expected_head(job)}):
                    continue
                if job["state"] == "pr_open":
                    ci.observe_and_route(db, job, cfg, lookup)
                elif job.get("ci_intervention") and not job.get("head_conflict"):
                    observation, _ = ci.observe(job, ci.expected_head(job), lookup)
                    if observation != job.get("ci"):
                        save(db, job, job["state"], ci=observation)
        except TaskStopped:
            continue
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            reason = clean(str(exc))
            if (job.get("state") == "pr_open" and job.get("ci_retry_at", 0) <= time.time()
                    and lease_active(db, job)):
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
                and job.get("ci_retry_at", 0) <= time.time()):
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
    repository_ids = {}
    errors = scan(cfg, db, repository_ids=repository_ids) + observe(db, api, cfg, repository_ids)
    errors += poll_ci_jobs(db, cfg)
    if workers is not None:
        return errors + workers.dispatch(db, cfg, config, root, once=once)
    workers = Workers()
    try:
        return errors + workers.dispatch(db, cfg, config, root, once=once) + workers.reap(wait=True)
    finally:
        workers.close()


def resume(db, cfg, target, allow_protected_paths=(), reuse_workspace=None, *,
           accept_head=None, add_rounds=None, evidence=None):
    slug, number = target.rsplit("#", 1)
    row = db.execute(
        "SELECT data FROM jobs WHERE slug=? AND number=? AND state IN ('needs_input','blocked')",
        (slug, int(number)),
    ).fetchone()
    if not row or slug not in {r["slug"] for r in cfg["repositories"] if r["enabled"]}:
        raise ValueError("Trabajo no reanudable")
    token = uuid.uuid4().hex
    job = json.loads(row[0])
    if not claim(db, job, token, 300):
        raise ValueError("El trabajo tiene un intento activo")
    try:
        job = leased_job(db, job, token)
        if job["state"] not in {"needs_input", "blocked"}:
            raise ValueError("Trabajo no reanudable")
        return resume_job(db, cfg, job, allow_protected_paths, reuse_workspace,
                          accept_head, add_rounds, evidence)
    finally:
        with db:
            db.execute("DELETE FROM leases WHERE token=?", (token,))


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


def resume_job(db, cfg, j, allow_protected_paths, reuse_workspace,
               accept_head, add_rounds, evidence):
    slug, number = j["slug"], j["number"]
    current = next(r for r in cfg["repositories"] if r["slug"] == slug)
    if j["repo"]["routing"] != current["routing"]:
        j.setdefault("routing_history", []).append(j["repo"]["routing"])
        j["repo"]["routing"] = current["routing"]
        j.pop("review", None)
    issue = api(f"repos/{slug}/issues/{number}")
    if j.get("pr_number") and not eligible(issue, j["repo"]):
        save(db, j, "cancelled", reason="Issue cerrada o etiqueta retirada")
        return
    if j.get("pr_number"):
        pr = ci.pull(j, api)
        if pr["state"] != "open":
            ci.validate(db, j, pr, set())
            return
    if accept_head or add_rounds is not None:
        if (not j.get("pr_number") or fingerprint(issue) != j["fingerprint"]):
            raise ValueError("La decisión requiere un PR vinculado y el alcance vigente")
    if accept_head:
        if not j.get("head_conflict") or j["head_conflict"]["observed"] != accept_head:
            raise ValueError("Autoriza el SHA del conflicto visible en status")
        approved_head(j, accept_head)
        entry = decision(j, "head", evidence, previous=ci.expected_head(j), head=accept_head,
                         candidate=j.get("candidate"), pr=j["pr_number"],
                         conflict=j["head_conflict"])
        additions = [entry]
        if add_rounds is not None:
            additions.append(rounds_decision(j, cfg, add_rounds, evidence, accept_head))
        save(db, j, "pr_open", decisions=[*j.get("decisions", []), *additions],
             published_head=accept_head, candidate=accept_head, head_conflict=None,
             repair_intent=None, ci_repair=None, ci_retry_at=0, ci_intervention=False,
             checks={}, review={}, build_workspace=None, reason="",
             ci={"sha": accept_head, "state": "waiting", "checks": [], "pending_runs": []})
        return
    scope_changed = fingerprint(issue) != j["fingerprint"]
    if j.get("head_conflict") and not scope_changed:
        raise ValueError("La discrepancia de head requiere resume --accept-head SHA --evidence TEXTO")
    if add_rounds is not None:
        head = ci.expected_head(j)
        approved_head(j, head)
        entry = rounds_decision(j, cfg, add_rounds, evidence, head)
        repair = j.get("ci_repair") or {}
        if j.get("retry") == "pr_open" and repair:
            ci_handled = [item for item in j.get("ci_handled", [])
                          if (item["sha"], item["key"]) != (repair["sha"], repair["key"])]
            save(db, j, "pr_open", decisions=[*j.get("decisions", []), entry],
                 ci_handled=ci_handled, ci_repair=None, ci_intervention=False, reason="")
        else:
            save(db, j, j.get("retry", "building"), decisions=[*j.get("decisions", []), entry],
                 ci_intervention=False, reason="")
        return
    if allow_protected_paths:
        if (
            j["state"] != "blocked"
            or not j.get("candidate")
            or not eligible(issue, j["repo"])
            or fingerprint(issue) != j["fingerprint"]
        ):
            raise ValueError(
                "La autorización requiere un trabajo bloqueado con candidato y alcance vigente"
            )
        if j.get("pr_number"):
            approved_head(j, ci.expected_head(j))
        approvals = dict(j.get("path_approvals", {}))
        for path in allow_protected_paths:
            normalized = PurePosixPath(path)
            if (
                not path
                or normalized.is_absolute()
                or str(normalized) != path
                or any(part in (".", "..", ".git") for part in normalized.parts)
                or any(char in path for char in "*?[]\\")
                or not any(
                    fnmatch.fnmatchcase(path, pattern)
                    for pattern in j["repo"].get("protected_paths", [])
                )
            ):
                raise ValueError(
                    "Indica una ruta protegida exacta relativa al repositorio"
                )
            approvals[path] = {
                "candidate": j["candidate"],
                "fingerprint": j["fingerprint"],
                "at": datetime.now(timezone.utc).isoformat(),
                "operator": operator_name(),
                "evidence": ci.excerpt(evidence or j.get("reason") or path),
            }
    if reuse_workspace is not None:
        if (
            j["state"] != "blocked"
            or j.get("retry") != "building"
            or not eligible(issue, j["repo"])
            or fingerprint(issue) != j["fingerprint"]
        ):
            raise ValueError(
                "La recuperación requiere construcción bloqueada y alcance vigente"
            )
        jobs = Path(
            os.environ.get("MASF_JOBS_DIR") or xdg("DATA", ".local/share") / "jobs"
        )
        folder = saved_workspace(
            jobs / f"{j['repo_id']}-{j['number']}", reuse_workspace
        )
        checkpoint = {
            "path": str(folder),
            "input_sha": j.get("candidate") or j["base"],
            "fingerprint": j["fingerprint"],
            "error": j.get("reason"),
        }
    if not eligible(issue, j["repo"]):
        save(db, j, "cancelled")
    elif j.get("pr_number") and not scope_changed and j.get("ci_intervention") and j.get("retry") != "planning":
        if j.get("retry") == "building" or (
            (j.get("ci_repair") or {}).get("diagnostics")
            and all(item["kind"] == "functional" for item in j["ci_repair"]["diagnostics"])
        ):
            raise ValueError("El flujo necesita resume --add-rounds N --evidence TEXTO")
        entry = decision(j, "resume", evidence or j["reason"], pr=j["pr_number"],
                         head=ci.expected_head(j), candidate=j.get("candidate"))
        save(db, j, "pr_open", decisions=[*j.get("decisions", []), entry],
             ci_repair=None, ci_intervention=False, ci_retry_at=0, reason="")
    elif scope_changed or j["state"] == "needs_input":
        published = ci.expected_head(j)
        for key in (
            "plan",
            "candidate",
            "checks",
            "review",
            "feedback",
            "build_workspace",
        ):
            j.pop(key, None)
        linked = {}
        if j.get("pr_number"):
            repair = j.get("ci_repair") or {}
            linked = dict(candidate=published, published_head=published, ci_repair=None,
                          ci_intervention=False, repair_intent=None, ci_retry_at=0, ci_handled=[])
            if not scope_changed and repair:
                linked["feedback"] = {"source": "CI", "sha": repair["sha"],
                                      "diagnostics": repair["diagnostics"]}
        save(
            db,
            j,
            "planning",
            **linked,
            title=issue["title"],
            body=issue["body"],
            rounds=0 if scope_changed or not j.get("pr_number") else j["rounds"],
            fingerprint=fingerprint(issue),
            scope_conflict=None,
            reason="",
        )
    else:
        updates = {"reason": "", "ci_retry_at": 0}
        if allow_protected_paths:
            updates["path_approvals"] = approvals
        if reuse_workspace is not None:
            updates["build_workspace"] = checkpoint
        save(db, j, j.get("retry", "queued"), **updates)



def rounds_decision(job, cfg, count, evidence, head):
    counts = ci.limits(job, cfg)
    if count <= 0 or counts["rounds"] < counts["build_limit"]:
        raise ValueError("Indica rondas adicionales positivas para un límite agotado")
    return decision(job, "rounds", evidence, rounds=count, pr=job["pr_number"], head=head,
                    candidate=job.get("candidate"), counters=counts)


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
    params = target or ()
    pull_filter = " WHERE slug=? COLLATE NOCASE AND number=?" if target else ""
    for row in db.execute("SELECT data FROM pull_requests" + pull_filter + " ORDER BY slug,number", params):
        pr = load_row(row)
        ident = pr["repo_id"], pr["number"]
        label = {"repo_id": pr["repo_id"], "number": pr["number"]}
        if not history:
            ci = load_row(db.execute(
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
            print(json.dumps({"type": "pull_event", **label, **dict(item), "detail": json.loads(item["detail"])}))
        for item in db.execute(
            "SELECT sha,data FROM pull_ci WHERE repo_id=? AND number=?" + sha_filter + " ORDER BY sha",
            params_with_sha,
        ):
            print(json.dumps({"type": "pull_evidence", **label, "sha": item["sha"], "ci": load_row(item)}))

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


def main():
    parser = argparse.ArgumentParser(prog="masf")
    parser.add_argument(
        "--config", type=Path, default=xdg("CONFIG", ".config") / "config.toml"
    )
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
    decisions = resume_parser.add_argument_group("decisiones para CI")
    decisions.add_argument("--accept-head")
    decisions.add_argument("--add-rounds", type=int)
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
    args = parser.parse_args()
    config = args.config.absolute()
    db = None
    try:
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
                        print(json.dumps(j | {"limits": ci.limits(j, cfg)}, ensure_ascii=False))
                        continue
                    counts = ci.limits(j, cfg)
                    print(
                        f"{j['slug']}#{j['number']} {j['state']}",
                        j.get("candidate", ""),
                        j.get("pr", ""),
                        f"CI {observed.get('state', '—')} {observed.get('sha', '')}",
                        f"rondas {counts['rounds']}/{counts['build_limit']}",
                        f"reintentos técnicos {counts['technical']}/{counts['technical_limit']}",
                        j.get("reason", ""),
                        j.get("ci_poll_error", ""),
                    )
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
                print(json.dumps(dict(row) | {"detail": json.loads(row["detail"])}, ensure_ascii=False))
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
                        if args.cmd == "resume":
                            if (args.accept_head or args.add_rounds is not None) and (
                                args.allow_protected_path or args.reuse_workspace
                            ):
                                raise ValueError("Ejecuta las autorizaciones de rutas y de CI por separado")
                            resume(
                                db,
                                cfg,
                                args.issue,
                                args.allow_protected_path,
                                args.reuse_workspace,
                                accept_head=args.accept_head,
                                add_rounds=args.add_rounds,
                                evidence=args.evidence,
                            )
                            return 0
                        if args.cmd == "link-pr":
                            if args.linked_issue is not None:
                                if args.pr is not None or args.head or args.evidence:
                                    raise ValueError("La vinculación de observación usa solo PR e issue")
                                link_pull(db, args.target, args.linked_issue)
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
