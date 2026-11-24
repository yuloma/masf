import argparse
import fcntl
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import load, xdg
from .queue import api, claim, connect, eligible, fingerprint, renew, save, scan
from .system import clean, command, docker_env
from .worker import Worker


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
    if not claim(db, job, token, ttl):
        db.close()
        return 0
    job["attempt"] = token

    def heartbeat():
        conn = connect(root)
        try:
            while not stop.wait(ttl / 3):
                if not renew(conn, job, token, ttl):
                    return
        finally:
            conn.close()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        jobs = Path(
            os.environ.get("MASF_JOBS_DIR") or xdg("DATA", ".local/share") / "jobs"
        )
        Worker(db, cfg, config, root, jobs, job).process()
        return 0
    except Exception as exc:
        reason = clean(str(exc))
        save(db, job, "blocked", retry=job["state"], reason=reason)
        print(reason, file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        thread.join()
        with db:
            db.execute("DELETE FROM leases WHERE token=?", (token,))
        db.close()


def tick(db, cfg, config, root, once):
    errors = scan(cfg, db)
    active = {repo["slug"] for repo in cfg["repositories"] if repo["enabled"]}
    for row in db.execute("SELECT data FROM jobs WHERE state='pr_open'").fetchall():
        job = json.loads(row[0])
        if job["slug"] in active:
            pr = api(f"repos/{job['slug']}/pulls/{job['pr_number']}")
            if pr["state"] == "closed":
                job.pop("attempt", None)
                save(db, job, "merged" if pr["merged_at"] else "closed")
    limit = 1 if once else cfg["service"]["max_parallel_jobs"]
    pending = []
    for row in db.execute(
        "SELECT data FROM jobs WHERE state IN ('queued','planning','building','checking','reviewing','publishing') ORDER BY rowid"
    ):
        job = json.loads(row[0])
        if job["slug"] in active:
            pending.append(job)
        if len(pending) >= limit:
            break
    with ThreadPoolExecutor(limit) as pool:
        futures = [pool.submit(process_task, cfg, config, root, job) for job in pending]
        errors += sum(future.result() for future in futures)
    return errors


def resume(db, cfg, target):
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
    if not eligible(issue, j["repo"]):
        save(db, j, "cancelled")
    elif fingerprint(issue) != j["fingerprint"] or j["state"] == "needs_input":
        for key in ("plan", "candidate", "checks", "review", "feedback"):
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
        save(db, j, j.get("retry", "queued"), reason="")


def main():
    parser = argparse.ArgumentParser(prog="masf")
    parser.add_argument(
        "--config", type=Path, default=xdg("CONFIG", ".config") / "config.toml"
    )
    subs = parser.add_subparsers(dest="cmd", required=True)
    subs.add_parser("doctor").add_argument("--offline", action="store_true")
    subs.add_parser("scan").add_argument("--dry-run", action="store_true")
    subs.add_parser("run").add_argument("--once", action="store_true", required=True)
    subs.add_parser("resume").add_argument("issue")
    subs.add_parser("status")
    subs.add_parser("serve")
    args = parser.parse_args()
    config = args.config.absolute()
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
            for row in db.execute("SELECT data FROM jobs ORDER BY rowid"):
                j = json.loads(row[0])
                print(
                    f"{j['slug']}#{j['number']} {j['state']}",
                    j.get("candidate", ""),
                    j.get("pr", ""),
                    j.get("reason", ""),
                )
            return 0
        with (root / "worker.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if args.cmd == "resume":
                        resume(db, cfg, args.issue)
                        return 0
                    errors = tick(db, load(config), config, root, args.cmd == "run")
                except Exception as exc:
                    if args.cmd != "serve":
                        raise
                    print(clean(str(exc)), file=sys.stderr, flush=True)
                    errors = 1
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                if args.cmd == "run":
                    return bool(errors)
                time.sleep(cfg["service"]["poll_seconds"] * (2 if errors else 1))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(clean(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
