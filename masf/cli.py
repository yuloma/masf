import argparse
import fnmatch
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

from .config import ConfigError, load, xdg
from .observe import link_pull, load_row, observe
from .queue import (
    api,
    claim,
    connect,
    eligible,
    fingerprint,
    renew,
    retry_delay,
    save,
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
        if current is None or current["state"] not in RUNNABLE:
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
        save(db, job, "blocked", retry=job["state"], reason=reason)
        print(reason, file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        with db:
            db.execute("DELETE FROM leases WHERE token=?", (token,))
        db.close()


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
        if job["slug"] in active and key not in running:
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
    if workers is not None:
        return errors + workers.dispatch(db, cfg, config, root, once=once)
    workers = Workers()
    try:
        return errors + workers.dispatch(db, cfg, config, root, once=once) + workers.reap(wait=True)
    finally:
        workers.close()


def resume(db, cfg, target, allow_protected_paths=(), reuse_workspace=None):
    slug, number = target.rsplit("#", 1)
    row = db.execute(
        "SELECT data FROM jobs WHERE slug=? AND number=? AND state IN ('needs_input','blocked')",
        (slug, int(number)),
    ).fetchone()
    if not row or slug not in {r["slug"] for r in cfg["repositories"] if r["enabled"]}:
        raise ValueError("Trabajo no reanudable")
    j = json.loads(row[0])
    j.pop("attempt", None)
    current = next(r for r in cfg["repositories"] if r["slug"] == slug)
    if j["repo"]["routing"] != current["routing"]:
        j.setdefault("routing_history", []).append(j["repo"]["routing"])
        j["repo"]["routing"] = current["routing"]
        j.pop("review", None)
    issue = api(f"repos/{slug}/issues/{number}")
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
    elif fingerprint(issue) != j["fingerprint"] or j["state"] == "needs_input":
        for key in (
            "plan",
            "candidate",
            "checks",
            "review",
            "feedback",
            "build_workspace",
        ):
            j.pop(key, None)
        save(
            db,
            j,
            "planning",
            title=issue["title"],
            body=issue["body"],
            rounds=0,
            fingerprint=fingerprint(issue),
            reason="",
        )
    else:
        updates = {"reason": ""}
        if allow_protected_paths:
            updates["path_approvals"] = approvals
        if reuse_workspace is not None:
            updates["build_workspace"] = checkpoint
        save(db, j, j.get("retry", "queued"), **updates)


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
    subs.add_parser("status").add_argument("--pr", type=pull_target)
    logs = subs.add_parser("logs")
    logs.add_argument("--pr", type=pull_target)
    logs.add_argument("--sha")
    link = subs.add_parser("link-pr")
    link.add_argument("pr", type=pull_target)
    link.add_argument("issue", type=pull_target)
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
                    j = load_row(row)
                    print(
                        f"{j['slug']}#{j['number']} {j['state']}",
                        j.get("candidate", ""),
                        j.get("pr", ""),
                        j.get("reason", ""),
                    )
            pull_output(db, args.pr)
            return 0
        if args.cmd == "logs":
            pull_output(db, args.pr, history=True, sha=args.sha)
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
                            resume(db, cfg, args.issue, args.allow_protected_path, args.reuse_workspace)
                            return 0
                        if args.cmd == "link-pr":
                            link_pull(db, args.pr, args.issue)
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
