import hashlib
import json
import sqlite3
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from .system import ProcessError, command


_retry_at = 0


def retry_delay():
    return max(0, _retry_at - time.monotonic())


def response_body(output):
    headers = {}
    while output.startswith("HTTP/"):
        head, separator, output = output.replace("\r\n", "\n").partition("\n\n")
        if not separator:
            raise ValueError("Respuesta HTTP incompleta de GitHub")
        for line in head.splitlines()[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.lower()] = value.strip()
    return headers, output


def api(endpoint, *opts):
    global _retry_at
    if retry_delay():
        raise RuntimeError(
            f"GitHub: límite de peticiones, esperar {retry_delay():.0f}s"
        )
    try:
        output = command(["gh", "api", "--include", endpoint, *opts])
    except ProcessError as exc:
        headers, _ = response_body(exc.stdout)
        if (
            headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in headers
            or "HTTP 429" in exc.stderr
        ):
            wait = 60
            if "retry-after" in headers:
                try:
                    wait = float(headers["retry-after"])
                except ValueError:
                    wait = (
                        parsedate_to_datetime(headers["retry-after"]).timestamp()
                        - time.time()
                    )
            elif "x-ratelimit-reset" in headers:
                wait = float(headers["x-ratelimit-reset"]) - time.time()
            _retry_at = time.monotonic() + max(1, wait)
        raise
    _, body = response_body(output)
    return json.loads(body)


def eligible(issue, repo):
    return (
        "pull_request" not in issue
        and issue["state"] == "open"
        and repo["label"] in {label["name"] for label in issue["labels"]}
    )


def fingerprint(issue):
    return hashlib.sha256(
        json.dumps([issue["title"], issue["body"]]).encode()
    ).hexdigest()


def latest(slug, branch):
    return api(f"repos/{slug}/commits/{quote(branch, safe='')}")["sha"]


def connect(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(root / "queue.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        repo_id INTEGER, number INTEGER, slug TEXT, state TEXT, data TEXT,
        PRIMARY KEY(repo_id, number))""")
    db.execute("""CREATE TABLE IF NOT EXISTS leases (
        repo_id INTEGER, number INTEGER, token TEXT, expires INTEGER,
        PRIMARY KEY(repo_id,number))""")
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, repo_id INTEGER,
        number INTEGER, attempt TEXT, state TEXT, detail TEXT)""")
    return db


def save(db, job, state, **updates):
    next_job = job | updates | {"state": state}
    with db:
        db.execute("BEGIN IMMEDIATE")
        if job.get("attempt"):
            active = db.execute(
                "SELECT 1 FROM leases WHERE repo_id=? AND number=? AND token=? AND expires>unixepoch()",
                (job["repo_id"], job["number"], job["attempt"]),
            ).fetchone()
            if not active:
                raise RuntimeError("La reserva del trabajo ha vencido")
        db.execute(
            "INSERT INTO events(repo_id,number,attempt,state,detail) VALUES(?,?,?,?,?)",
            (
                job["repo_id"],
                job["number"],
                job.get("attempt"),
                state,
                json.dumps(updates),
            ),
        )
        db.execute(
            "UPDATE jobs SET state=?,data=? WHERE repo_id=? AND number=?",
            (state, json.dumps(next_job), job["repo_id"], job["number"]),
        )
    job.update(next_job)
    print(f"{job['slug']}#{job['number']} {state}", flush=True)


def scan(cfg, db=None):
    errors = 0
    for repo in cfg["repositories"]:
        if not repo["enabled"]:
            continue
        slug = repo["slug"]
        try:
            meta = api(f"repos/{slug}")
            branch = repo.get("base_branch") or meta["default_branch"]
            page = 1
            base = None
            while True:
                issues = api(
                    f"repos/{slug}/issues?state=open&labels={quote(repo['label'], safe='')}&per_page=100&page={page}"
                )
                for issue in issues:
                    if not eligible(issue, repo):
                        continue
                    number = issue["number"]
                    if db is None:
                        print(f"{slug}#{number} elegible ({repo['label']})")
                        continue
                    base = base or latest(slug, branch)
                    job = dict(
                        repo_id=meta["id"],
                        slug=slug,
                        number=number,
                        state="queued",
                        repo=repo,
                        branch=branch,
                        base=base,
                        title=issue["title"],
                        body=issue["body"],
                        fingerprint=fingerprint(issue),
                        rounds=0,
                    )
                    with db:
                        result = db.execute(
                            "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?)",
                            (meta["id"], number, slug, "queued", json.dumps(job)),
                        )
                    if result.rowcount:
                        print(f"{slug}#{number} queued", flush=True)
                    else:
                        print(f"{slug}#{number} ya registrado", flush=True)
                if len(issues) < 100:
                    break
                page += 1
        except (RuntimeError, OSError) as exc:
            print(f"{slug}: {exc}", file=sys.stderr, flush=True)
            errors += 1
    return errors


def claim(db, job, token, ttl):
    with db:
        result = db.execute(
            """INSERT INTO leases VALUES(?,?,?,unixepoch()+?)
            ON CONFLICT(repo_id, number) DO UPDATE SET token=excluded.token, expires=excluded.expires
            WHERE leases.expires<=unixepoch()""",
            (job["repo_id"], job["number"], token, ttl),
        )
        return bool(result.rowcount)


def renew(db, job, token, ttl):
    with db:
        return db.execute(
            """UPDATE leases SET expires=unixepoch()+? WHERE
            repo_id=? AND number=? AND token=? AND expires>unixepoch()""",
            (ttl, job["repo_id"], job["number"], token),
        ).rowcount
