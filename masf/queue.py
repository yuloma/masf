import hashlib
import json
import sqlite3
import sys
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from .evidence import explanation, model, provenance, timestamp
from .system import ProcessError, TaskStopped, command, filtered


_retry_at = 0
_rate_lock = threading.Lock()


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


def http_status(exc):
    if isinstance(exc, ProcessError):
        first = exc.stdout.partition("\n")[0].split()
        if len(first) > 1 and first[0].startswith("HTTP/") and first[1].isdigit():
            return int(first[1])
    return None


def set_retry(headers, *, limited=False):
    global _retry_at
    if not (limited or "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"):
        return
    try:
        if "retry-after" in headers:
            try:
                wait = float(headers["retry-after"])
            except ValueError:
                wait = parsedate_to_datetime(headers["retry-after"]).timestamp() - time.time()
        else:
            wait = float(headers.get("x-ratelimit-reset", time.time() + 60)) - time.time()
    except (ValueError, TypeError, OverflowError):
        wait = 60
    with _rate_lock:
        _retry_at = max(_retry_at, time.monotonic() + max(1, wait))


def api(endpoint, *opts, stdin=None, raw=False, with_headers=False):
    if retry_delay():
        raise RuntimeError(
            f"GitHub: límite de peticiones, esperar {retry_delay():.0f}s"
        )
    try:
        options = {"stdin": stdin} if stdin is not None else {}
        output = command(["gh", "api", "--include", endpoint, *opts], **options)
    except ProcessError as exc:
        headers, _ = response_body(exc.stdout)
        limited = (
            http_status(exc) == 429
            or "HTTP 429" in exc.stderr
            or "secondary rate limit" in (exc.stdout + exc.stderr).lower()
            or "rate limit exceeded" in (exc.stdout + exc.stderr).lower()
        )
        set_retry(headers, limited=limited)
        raise
    headers, body = response_body(output)
    set_retry(headers)
    result = body if raw else json.loads(body)
    return (headers, result) if with_headers else result


def pages(request, endpoint, key=None):
    separator = "&" if "?" in endpoint else "?"
    page = 1
    while True:
        headers, result = request(
            f"{endpoint}{separator}per_page=100&page={page}", with_headers=True,
        )
        items = result[key] if key else result
        if not isinstance(items, list):
            raise ValueError("Página de GitHub inválida")
        yield from items
        links = headers.get("link")
        has_next = 'rel="next"' in links if links is not None else len(items) == 100
        if not has_next:
            if key and result.get("total_count", 0) > (page - 1) * 100 + len(items):
                raise RuntimeError("GitHub devolvió un listado incompleto")
            return
        page += 1


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
    db.execute("""CREATE INDEX IF NOT EXISTS events_job_lookup ON events(repo_id, number, id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS pull_requests (
        repo_id INTEGER, number INTEGER, slug TEXT, state TEXT, sha TEXT, data TEXT,
        PRIMARY KEY(repo_id, number))""")
    db.execute("""CREATE TABLE IF NOT EXISTS pull_ci (
        repo_id INTEGER, number INTEGER, sha TEXT, data TEXT,
        PRIMARY KEY(repo_id, number, sha))""")
    db.execute("""CREATE TABLE IF NOT EXISTS pull_events (
        id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, repo_id INTEGER,
        number INTEGER, sha TEXT, kind TEXT, detail TEXT)""")
    db.execute("""CREATE INDEX IF NOT EXISTS pull_events_lookup
        ON pull_events(repo_id, number, sha, id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS pull_sources (
        slug TEXT PRIMARY KEY, error TEXT, changed_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS pull_source_events (
        id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, slug TEXT,
        source TEXT, error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS pull_links (
        repo_id INTEGER, number INTEGER, job_number INTEGER,
        PRIMARY KEY(repo_id, number), UNIQUE(repo_id, job_number))""")
    db.execute("""CREATE TABLE IF NOT EXISTS stage_attempts (
        id TEXT PRIMARY KEY, repo_id INTEGER, number INTEGER, role TEXT, status TEXT, data TEXT)""")
    db.execute("""CREATE INDEX IF NOT EXISTS stage_attempts_job
        ON stage_attempts(repo_id, number, role)""")
    return db


def write_event(db, job, state, updates):
    db.execute(
        "INSERT INTO events(repo_id,number,attempt,state,detail) VALUES(?,?,?,?,?)",
        (job["repo_id"], job["number"], job.get("attempt"), state, json.dumps(filtered(updates))),
    )


def store_stage(db, record):
    safe = filtered(record)
    db.execute(
        """INSERT INTO stage_attempts VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data""",
        (safe["id"], safe["repo_id"], safe["number"], safe["role"], safe["status"],
         json.dumps(safe)),
    )


def stage_attempts(db, job, *, role=None):
    return [json.loads(row["data"]) for row in db.execute(
        """SELECT data FROM stage_attempts WHERE repo_id=? AND number=?
        AND (? IS NULL OR role=?) ORDER BY rowid""",
        (job["repo_id"], job["number"], role, role),
    )]


def record_stage(db, job, record, *, boundary=None):
    if db is None:
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        require_lease(db, job)
        store_stage(db, record)
        if boundary:
            write_event(db, job, job["state"], {"stage_event": boundary, "stage": record})


def interrupt_stages(db, job):
    for record in stage_attempts(db, job):
        if record["status"] != "active" or record["lease"] == job.get("attempt"):
            continue
        reason = "La ejecución anterior se interrumpió antes de registrar el cierre"
        record.update(status="interrupted", detected_at=timestamp(), error=reason,
                      explanation=explanation(reason, reason=reason,
                                              evidence=record["explanation"]["evidence"]))
        record_stage(db, job, record, boundary="interrupted")


def decision_details(entries, job):
    return [
        {"role": "operation", "executor": entry.get("operator"), "model": model(applicable=False),
         "sha": entry.get("head") or entry.get("candidate") or job.get("candidate") or job.get("base"),
         "at": entry.get("at"), "decision": entry,
         "explanation": explanation(entry["kind"], decisions=[entry["kind"]],
                                    reason=entry.get("evidence"), source="operator")}
        for entry in entries
    ]


def save(db, job, state, **updates):
    next_job = job | updates | {"state": state}
    persisted = {key: value for key, value in next_job.items() if key not in {"attempt", "_stage"}}
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not lease_active(db, job):
            raise RuntimeError("La reserva del trabajo ha vencido")
        detail = dict(updates)
        if "ci" in updates and state == job["state"] and not updates.get("ci_repair"):
            detail["kind"] = "ci_observation"
        if job.get("_stage"):
            detail["stage_attempt"] = job["_stage"]
        additions = updates.get("decisions", [])[len(job.get("decisions", [])):]
        if additions:
            detail["operations"] = decision_details(additions, next_job)
        build = updates.get("build") or {}
        if updates.get("candidate") and build.get("stage_attempt"):
            row = db.execute("SELECT data FROM stage_attempts WHERE id=? AND repo_id=? AND number=?",
                             (build["stage_attempt"], job["repo_id"], job["number"])).fetchone()
            if row:
                stage = json.loads(row["data"])
                stage["candidate_sha"] = updates["candidate"]
                if stage.get("ci_origin"):
                    stage["ci_result"] = {"pr": next_job.get("pr_number"), "sha": updates["candidate"]}
                store_stage(db, stage)
                detail["stage_attempt"] = stage["id"]
        write_event(db, job, state, detail)
        db.execute(
            "UPDATE jobs SET state=?,data=? WHERE repo_id=? AND number=?",
            (state, json.dumps(filtered(persisted)), job["repo_id"], job["number"]),
        )
    job.update(next_job)
    print(f"{job['slug']}#{job['number']} {state}", flush=True)


def save_link(db, job, approval):
    key = (job["repo_id"], job["number"])
    persisted = {key: value for key, value in job.items() if key not in {"attempt", "_stage"}}
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not lease_active(db, job):
            raise RuntimeError("La reserva del trabajo ha vencido")
        conflict = db.execute(
            "SELECT 1 FROM jobs WHERE repo_id=? AND json_extract(data, '$.pr_number')=?",
            (job["repo_id"], job["pr_number"]),
        ).fetchone()
        row = db.execute("SELECT state,data FROM jobs WHERE repo_id=? AND number=?", key).fetchone()
        if conflict or (row and row["state"] != "queued"):
            raise ValueError("El PR está vinculado o la issue ya tiene un trabajo activo")
        if row:
            old = json.loads(row["data"])
            persisted["provenance"] = old.get("provenance")
        db.execute(
            """INSERT INTO jobs VALUES (?,?,?,?,?)
            ON CONFLICT(repo_id, number) DO UPDATE SET state=excluded.state,data=excluded.data""",
            (*key, job["slug"], "pr_open", json.dumps(filtered(persisted))),
        )
        write_event(db, job, "pr_open", {"decisions": [approval], "ci": job["ci"],
                                           "operations": decision_details([approval], job),
                                           "provenance": job["provenance"]})
    print(f"{job['slug']}#{job['number']} pr_open", flush=True)


def scan(cfg, db=None, *, repository_ids=None):
    errors = 0
    for repo in cfg["repositories"]:
        if not repo["enabled"]:
            continue
        slug = repo["slug"]
        try:
            meta = api(f"repos/{slug}")
            if repository_ids is not None:
                repository_ids[slug] = meta["id"]
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
                    job["provenance"] = provenance(cfg, job)
                    with db:
                        result = db.execute(
                            "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?)",
                            (meta["id"], number, slug, "queued", json.dumps(filtered(job))),
                        )
                    if result.rowcount:
                        print(f"{slug}#{number} queued", flush=True)
                    else:
                        print(f"{slug}#{number} ya registrado", flush=True)
                if len(issues) < 100:
                    break
                page += 1
        except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            print(f"{slug}: {exc}", file=sys.stderr, flush=True)
            errors += 1
            if retry_delay():
                break
    return errors


def claim(db, job, token, ttl, limit=None):
    with db:
        result = db.execute(
            """INSERT INTO leases SELECT ?,?,?,unixepoch()+?
            WHERE ? IS NULL OR (SELECT count(*) FROM leases WHERE expires>unixepoch()) < ?
            ON CONFLICT(repo_id, number) DO UPDATE SET token=excluded.token, expires=excluded.expires
            WHERE leases.expires<=unixepoch()""",
            (job["repo_id"], job["number"], token, ttl, limit, limit),
        )
        return bool(result.rowcount)


def renew(db, job, token, ttl):
    with db:
        return db.execute(
            """UPDATE leases SET expires=unixepoch()+? WHERE
            repo_id=? AND number=? AND token=? AND expires>unixepoch()""",
            (ttl, job["repo_id"], job["number"], token),
        ).rowcount


def leased_job(db, job, token):
    row = db.execute(
        "SELECT data, state FROM jobs WHERE repo_id=? AND number=?",
        (job["repo_id"], job["number"]),
    ).fetchone()
    current = json.loads(row["data"])
    current.update(state=row["state"], attempt=token)
    return current


def lease_active(db, job):
    return not job.get("attempt") or db.execute(
        "SELECT 1 FROM leases WHERE repo_id=? AND number=? AND token=? AND expires>unixepoch()",
        (job["repo_id"], job["number"], job["attempt"]),
    ).fetchone() is not None


def require_lease(db, job):
    if not lease_active(db, job):
        raise TaskStopped("La reserva del trabajo ha vencido")
