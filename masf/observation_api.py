"""Loopback HTTP access to evidence and optionally to authenticated decisions."""

import argparse
import json
import os
import re
import sqlite3
import stat
import tomllib
from contextlib import ExitStack, closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from itertools import islice
from urllib.parse import parse_qsl

from . import ci
from . import observation_data as public
from .actions import ActionError, Actions
from .config import ConfigError
from .evidence import digest
from .queue import connect, decision_details

DEFAULT_PORT = 8765
PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_ARTIFACT_SIZE = 2 * 1024 * 1024
IDENT = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHA = re.compile(r"[0-9a-fA-F]{4,64}")
SLUG = re.compile(r"[\w.-]+/[\w.-]+")
STAGE_FILE = re.compile(r"(?:plan|build|review|checks|publish)-[A-Za-z0-9_-]+\.json")
CHECK_FILE = re.compile(r"[0-9a-f]{32}/checks\.json")
STATE = re.compile(r"[a-z_]{1,40}")
DASHBOARD_ASSETS = {
    "/dashboard/": ("index.html", "text/html"),
    "/dashboard/style.css": ("style.css", "text/css"),
    "/dashboard/app.js": ("app.js", "text/javascript"),
}


class RequestError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def missing():
    return RequestError(404, "not_found", "Recurso de observación no encontrado")


def invalid():
    return RequestError(400, "invalid_request", "Ruta, filtro o paginación inválidos")


def positive(value, *, zero=False):
    if not re.fullmatch(r"[0-9]{1,18}", value) or int(value) < (0 if zero else 1):
        raise invalid()
    return int(value)


def port(value):
    if re.fullmatch(r"[0-9]{1,5}", value) and 1 <= int(value) <= 65535:
        return int(value)
    raise argparse.ArgumentTypeError("se requiere un puerto entre 1 y 65535")


def registry(path):
    with path.open("rb") as source:
        cfg = tomllib.load(source)
    repos = cfg.get("repositories")
    service = cfg.get("service")
    if (type(cfg.get("version")) is not int or cfg["version"] != 1
            or not isinstance(repos, list) or not isinstance(service, dict)):
        raise ConfigError("La API de observación requiere el registro de la instalación v1")
    visible = {}
    for repo in repos:
        slug = public.mapping(repo).get("slug")
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise ConfigError("El registro de observación contiene un repositorio inválido")
        key = slug.lower()
        if key in visible:
            raise ConfigError("El registro de observación contiene un repositorio duplicado")
        visible[key] = public.fields(repo, "slug enabled label base_branch")
    return visible, {"service": {
        "max_build_rounds": service.get("max_build_rounds"),
        "max_ci_technical_retries": service.get("max_ci_technical_retries", 2),
    }}


def connect_readonly(root):
    db = sqlite3.connect((root / "queue.sqlite3").absolute().as_uri() + "?mode=ro",
                         uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
    except sqlite3.Error:
        db.close()
        raise
    return db


def decode(row):
    return json.loads(row["data"])


class Query:
    def __init__(self, value):
        try:
            pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True,
                              max_num_fields=8, errors="strict")
        except ValueError:
            raise invalid() from None
        self.filters = dict(pairs)
        if len(pairs) != len(self.filters):
            raise invalid()

    def take(self, key, pattern=None):
        value = self.filters.pop(key, None)
        if value is not None and (not value or pattern and not pattern.fullmatch(value)):
            raise invalid()
        return value

    def finish(self):
        if self.filters:
            raise invalid()

    def page(self):
        limit = positive(self.take("limit") or str(PAGE_SIZE))
        offset = positive(self.take("offset") or "0", zero=True)
        if limit > MAX_PAGE_SIZE:
            raise invalid()
        self.finish()
        return Page(limit, offset)


class Page:
    def __init__(self, limit, offset):
        self.limit, self.offset = limit, offset

    def take(self, entries, project):
        return self.result(list(islice(entries, self.offset, self.offset + self.limit + 1)), project)

    def sql(self, db, statement, params, project):
        rows = db.execute(statement + " LIMIT ? OFFSET ?", (*params, self.limit + 1, self.offset))
        return self.result(list(rows), project)

    def result(self, entries, project):
        return {"data": [project(item) for item in entries[:self.limit]],
                "pagination": {"limit": self.limit, "offset": self.offset,
                               "next_offset": self.offset + self.limit if len(entries) > self.limit else None}}


def artifact_kind(record, path):
    role, ident = record.get("role"), record.get("id")
    if path == record.get("evidence_file") and STAGE_FILE.fullmatch(path):
        if record.get("historical") or path == f"{role}-{ident}.json":
            return "stage"
    if role in {"plan", "build", "review"} and path == f"{role}-{ident}.activity.jsonl":
        return "activity"
    if role == "checks" and CHECK_FILE.fullmatch(path):
        return "checks"
    return None


def read_artifact(root, identity, path):
    # Pin each directory while opening the next component; never follow links.
    with ExitStack() as stack:
        dirs = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def open_fd(name, flags, parent=None):
            fd = os.open(name, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        parent = open_fd(root, dirs)
        parent = open_fd("evidence", dirs, parent)
        parent = open_fd(f"{identity[0]}-{identity[1]}", dirs, parent)
        *folders, filename = path.split("/")
        for folder in folders:
            parent = open_fd(folder, dirs, parent)
        fd = open_fd(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, parent)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise missing()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_ARTIFACT_SIZE + 1)
        if len(data) > MAX_ARTIFACT_SIZE:
            raise RequestError(413, "artifact_too_large", "La evidencia supera el límite de lectura")
        return data


class Stages:
    def __init__(self, db, job, identity, tables):
        self.records = {}
        if "stage_attempts" in tables:
            for row in db.execute(
                "SELECT id,data FROM stage_attempts WHERE repo_id=? AND number=? ORDER BY rowid", identity,
            ):
                self.records[row["id"]] = decode(row) | {"id": row["id"]}

        def add_historical(value):
            for role in ("plan", "build", "review"):
                record = public.mapping(value.get(role))
                if value.get(role + "_attempt") or not record:
                    continue
                ident = "legacy-" + digest([identity, role, record])
                stage = public.historical_stage(role, record, ident)
                if stage:
                    self.records.setdefault(ident, stage)

        if "events" in tables:
            for row in db.execute(
                """SELECT detail FROM events WHERE repo_id=? AND number=?
                AND (json_type(detail,'$.plan')='object' OR json_type(detail,'$.build')='object'
                     OR json_type(detail,'$.review')='object') ORDER BY id""", identity,
            ):
                add_historical(json.loads(row["detail"]))
        add_historical(job)
        self.artifacts = {}
        self.by_path = {}
        for ident, record in self.records.items():
            explanation = public.mapping(record.get("explanation"))
            paths = [record.get("evidence_file"), *public.items(explanation.get("evidence"))]
            for path in paths:
                if isinstance(path, str) and (kind := artifact_kind(record, path)):
                    artifact = digest([identity, ident, path])
                    self.artifacts[artifact] = (ident, kind, path)
                    self.by_path.setdefault(path, (artifact, ident))

    def references(self, record):
        refs = []
        explanation = public.mapping(record.get("explanation"))
        for ref in [record.get("evidence_file"), *public.items(explanation.get("evidence"))]:
            if not isinstance(ref, str):
                continue
            if ref in self.records:
                ident, artifact = ref, None
            elif ref in self.by_path:
                artifact, ident = self.by_path[ref]
            else:
                continue
            entry = {"attempt_id": ident, "artifact_id": artifact,
                     "sha": public.scalar(self.records[ident].get("input_sha"))}
            if entry not in refs:
                refs.append(entry)
        return refs

    def project(self, record):
        stage = public.stage(record)
        stage["explanation"]["evidence"] = self.references(record)
        return stage

    def artifact(self, root, identity, ident, page):
        if ident not in self.artifacts:
            raise missing()
        attempt, kind, path = self.artifacts[ident]
        record = self.records[attempt]
        try:
            raw = read_artifact(root, identity, path)
        except OSError:
            raise missing() from None
        metadata = {"artifact_id": ident, "attempt_id": attempt, "kind": kind,
                    "sha": public.scalar(record.get("input_sha"))}
        if kind == "activity":
            events = activity(raw, record)
            return page.take(events, lambda event: event) | {"artifact": metadata}
        if page is not None:
            raise invalid()
        value = public.mapping(json.loads(raw))
        if kind == "checks":
            if not value or value.get("sha") != record.get("input_sha"):
                raise missing()
            result = public.check_report(value)
        else:
            if record.get("historical"):
                if value.get("id") is not None or (
                    record.get("input_sha") and value.get("sha") != record["input_sha"]
                ):
                    raise missing()
                value = public.historical_stage(record["role"], value, attempt)
            elif (value.get("repo_id"), value.get("number"), value.get("id"), value.get("role")) != (
                *identity, attempt, record.get("role"),
            ):
                raise missing()
            result = self.project(value)
        return {"data": metadata | {"content": result}}


def activity(raw, record):
    # The writer appends complete JSON lines. A partial final write can be retried.
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        event = public.mapping(json.loads(line))
        if event.get("attempt") != record["id"] or event.get("sha") != record.get("input_sha"):
            continue
        yield public.fields(event, "at role sha attempt event type subtype provider version "
                                   "execution_id execution_id_source") | {
            "model": public.model(event["model"]) if public.mapping(event.get("model")) else None,
            "usage": public.usage(event["usage_evidence"]) if public.mapping(event.get("usage_evidence")) else None,
            "tools": public.strings(event.get("tools")),
        }


class View:
    def __init__(self, db, root, config):
        self.db, self.root = db, root
        self.repos, self.cfg = registry(config)
        self.tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if "jobs" not in self.tables:
            raise sqlite3.OperationalError("queue schema unavailable")

    def dispatch(self, path, query):
        resource, *rest = path
        if resource == "repositories" and not rest:
            return self.repositories(query)
        if resource == "jobs":
            return self.jobs(rest, query)
        if resource == "pulls":
            return self.pulls(rest, query)
        raise missing()

    def repositories(self, query):
        slug = query.take("slug", SLUG)
        page = query.page()
        known = {}
        tables = ["jobs", "pull_requests"] if "pull_requests" in self.tables else ["jobs"]
        for row in self.db.execute(" UNION ".join(
            f"SELECT DISTINCT lower(slug),repo_id FROM {table}" for table in tables
        )):
            known.setdefault(row[0], []).append(row[1])
        repos = (self.repos[key] | {
            "repo_id": known[key][0] if len(known.get(key, [])) == 1 else None,
            "known_repo_ids": known.get(key, []),
            "observation": self.source_errors(key),
        } for key in sorted(self.repos) if slug is None or key == slug.lower())
        return page.take(repos, lambda item: item)

    def source_errors(self, slug, number=None):
        if "pull_sources" in self.tables:
            row = self.db.execute(
                "SELECT error,changed_at FROM pull_sources WHERE slug=? COLLATE NOCASE", (slug,)
            ).fetchone()
            if row:
                errors = public.error_map(json.loads(row["error"]))
                if number is not None:
                    errors = {key: value for key, value in errors.items()
                              if not key.startswith("pull:") or key == f"pull:{number}"}
                return {"changed_at": row["changed_at"], "errors": errors}
        return {"changed_at": None, "errors": {}}

    def list_rows(self, table, query, filters, project):
        where, params = [], []
        for key, column, pattern, numeric in filters:
            value = query.take(key, pattern)
            if value is not None:
                where.append(column + "=?")
                params.append(positive(value) if numeric else value)
        page = query.page()
        if table not in self.tables:
            return page.result([], project)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return page.sql(self.db, f"SELECT * FROM {table}{clause} ORDER BY repo_id,number", params, project)

    def jobs(self, path, query):
        if not path:
            return self.list_rows("jobs", query, (
                ("repo_id", "repo_id", None, True), ("slug", "slug COLLATE NOCASE", SLUG, False),
                ("state", "state", STATE, False),
            ), self.job)
        if len(path) < 2:
            raise missing()
        identity = (positive(path[0]), positive(path[1]))
        row = self.db.execute("SELECT * FROM jobs WHERE repo_id=? AND number=?", identity).fetchone()
        if row is None:
            raise missing()
        tail = path[2:]
        match tail:
            case []:
                query.finish()
                return {"data": self.job(row)}
            case ["events"]:
                return self.job_events(identity, query)
            case ["attempts"] | ["attempts", _] | ["artifacts", _]:
                stages = Stages(self.db, decode(row), identity, self.tables)
                return self.stage_resource(stages, identity, tail, query)
            case _:
                raise missing()

    def stage_resource(self, stages, identity, path, query):
        match path:
            case ["attempts"]:
                role = query.take("role", STATE)
                status = query.take("status", STATE)
                return query.page().take((
                    record for record in stages.records.values()
                    if (role is None or record.get("role") == role)
                    and (status is None or record.get("status") == status)
                ), stages.project)
            case ["attempts", ident]:
                query.finish()
                if ident not in stages.records:
                    raise missing()
                return {"data": stages.project(stages.records[ident])}
            case ["artifacts", ident]:
                if not re.fullmatch(r"[a-f0-9]{64}", ident) or ident not in stages.artifacts:
                    raise missing()
                page = query.page() if stages.artifacts[ident][1] == "activity" else None
                query.finish()
                return stages.artifact(self.root, identity, ident, page)

    def job(self, row):
        job = decode(row)
        identity = row["repo_id"], row["number"]
        event_id = None
        if "events" in self.tables:
            event_id = self.db.execute(
                "SELECT max(id) FROM events WHERE repo_id=? AND number=?", identity,
            ).fetchone()[0]
        active = None
        latest = None
        question_attempt = None
        questions = []
        if "stage_attempts" in self.tables:
            latest = self.db.execute(
                """SELECT id,role,status,data FROM stage_attempts WHERE repo_id=? AND number=?
                ORDER BY rowid DESC LIMIT 1""", identity,
            ).fetchone()
            if latest and latest["status"] == "active":
                candidate = decode(latest)
                if candidate.get("lease") and "leases" in self.tables:
                    lease = self.db.execute(
                        """SELECT 1 FROM leases WHERE repo_id=? AND number=?
                        AND expires>unixepoch() AND token=?""", (*identity, candidate["lease"]),
                    ).fetchone()
                    if lease:
                        active = latest
                else:
                    active = latest
            if row["state"] == "needs_input" and job.get("plan_attempt"):
                question_attempt = self.db.execute(
                    """SELECT data FROM stage_attempts WHERE repo_id=? AND number=? AND id=?
                    AND status='needs_input'""", (*identity, job["plan_attempt"]),
                ).fetchone()
        reason = public.scalar(job.get("reason")) or None
        attempt = public.stage(decode(active) | {"id": active["id"]}) if active else None
        if question_attempt:
            question_value = public.scalar(public.mapping(decode(question_attempt).get("result")).get("question"))
            if question_value:
                questions.append({"attempt_id": job["plan_attempt"], "text": question_value})
        historical_question = public.mapping(public.mapping(job.get("plan")).get("result")).get("question")
        historical_question = historical_question or public.mapping(job.get("plan")).get("question")
        if row["state"] == "needs_input" and not questions and isinstance(historical_question, str):
            questions.append({"attempt_id": None, "text": public.scalar(historical_question)})
        cfg = self.cfg
        # Old jobs may have no fingerprint, which precludes any scoped round approval.
        limits_job = job | {"fingerprint": job.get("fingerprint"), "decisions": [
            item for item in public.items(job.get("decisions"))
            if public.mapping(item).get("kind") == "rounds" and type(item.get("rounds")) is int
            and item.get("fingerprint") is not None
        ]}
        counts = ci.limits(limits_job, cfg) if type(cfg["service"]["max_build_rounds"]) is int else {}
        limits = public.fields(counts, "rounds functional build_limit technical technical_limit")
        pr_number = job.get("pr_number") if type(job.get("pr_number")) is int else None
        if pr_number is None:
            match = re.fullmatch(r"https://github\.com/" + re.escape(row["slug"]) + r"/pull/(\d+)",
                                 public.url(job.get("pr")) or "", re.IGNORECASE)
            if match:
                pr_number = int(match[1])
            elif "pull_links" in self.tables:
                link = self.db.execute(
                    "SELECT number FROM pull_links WHERE repo_id=? AND job_number=?", identity,
                ).fetchone()
                if link:
                    pr_number = link["number"]
        return public.fields(dict(row), "repo_id slug state") | {
            "number": identity[1], "title": public.scalar(job.get("title")),
            "issue": {"number": identity[1], "url": public.url(
                f"https://github.com/{row['slug']}/issues/{identity[1]}"
            )},
            "pr": {"number": pr_number, "url": public.url(job.get("pr")) or public.url(
                f"https://github.com/{row['slug']}/pull/{pr_number}"
            )} if pr_number is not None else None,
            "stage": active["role"] if active else JOB_STAGE.get(row["state"]) or (
                latest["role"] if latest and row["state"] in {"blocked", "needs_input", "retry_wait"} else None
            ),
            "active_attempt": attempt, "last_attempt_id": public.scalar(latest["id"]) if latest else None,
            "last_event_id": event_id,
            "revision": public.revision(job, event_id),
            "base_sha": public.scalar(job.get("base")), "candidate_sha": public.scalar(job.get("candidate")),
            "published_sha": public.scalar(job.get("published_head")),
            "reason": reason, "checks": public.check_report(job.get("checks")),
            "intervention": {
                "pending": row["state"] in {"blocked", "needs_input"},
                "reason": reason if row["state"] in {"blocked", "needs_input"} else None,
                "questions": questions,
                "scope_changed": bool(job.get("scope_conflict")),
                "protected_paths": (public.strings(job.get("blocked_paths"))
                                    or public.protected_paths(job.get("reason")))
                if row["state"] == "blocked" and public.protected_paths(job.get("reason")) else [],
                "failed_attempt": public.scalar(job.get("failed_attempt")),
                "build_checkpoint": public.build_checkpoint(job) if row["state"] in {"blocked", "needs_input"} else None,
                "ci": bool(job.get("ci_intervention")) and row["state"] in {"blocked", "needs_input"},
                "retry_stage": JOB_STAGE.get(job.get("retry")),
                "head_conflict": public.fields(job["head_conflict"], "expected observed remote")
                if public.mapping(job.get("head_conflict")) else None,
            },
            "limits": limits, "ci": public.local_ci(job.get("ci")),
            "ci_poll_error": public.observation_error(job["ci_poll_error"]) if job.get("ci_poll_error") else None,
            "repair": public.repair(job.get("ci_repair")),
            "retry_at": public.scalar(job.get("ci_retry_at")) or None,
        }

    def job_events(self, identity, query):
        state = query.take("state", STATE)
        page = query.page()
        if "events" not in self.tables:
            return page.result([], lambda item: item)
        where = " AND state=?" if state else ""
        params = (*identity, state) if state else identity
        return page.sql(self.db,
                        "SELECT id,at,state,detail FROM events WHERE repo_id=? AND number=?"
                        + where + " ORDER BY id", params, lambda row: self.job_event(row, identity))

    def job_event(self, row, identity):
        detail = public.mapping(json.loads(row["detail"]))
        historic = []
        for role in ("plan", "build", "review"):
            record = public.mapping(detail.get(role))
            if record and not detail.get(role + "_attempt"):
                stage = public.historical_stage(role, record, "legacy-" + digest([identity, role, record]))
                if stage:
                    historic.append(public.stage(stage))
        operations = public.items(detail.get("operations"))
        if not operations and detail.get("decisions"):
            entries = [entry for entry in public.items(detail.get("decisions"))
                       if isinstance(public.mapping(entry).get("kind"), str)]
            operations = decision_details(entries, detail)
        stage = public.mapping(detail.get("stage"))
        return public.fields(dict(row), "id at state") | {
            "detail": public.fields(detail, "kind reason stage_event stage_attempt base candidate sha pr_number retry"),
            "operations": [public.operation(item) for item in operations if public.mapping(item)],
            "path_approvals": [
                {"path": public.scalar(path), **public.fields(item, "at candidate sha operator")}
                for path, item in public.mapping(detail.get("path_approvals")).items()
            ],
            "feedback": public.feedback(detail.get("feedback")),
            "checks": public.check_report(detail.get("checks")),
            "repair": public.repair(detail.get("ci_repair")),
            "stage": public.stage(stage) if stage else None,
            "historical_stages": historic,
            "ci": public.local_ci(detail.get("ci")),
            "ci_poll_error": public.observation_error(detail["ci_poll_error"]) if detail.get("ci_poll_error") else None,
        }

    def pulls(self, path, query):
        if not path:
            return self.list_rows("pull_requests", query, (
                ("repo_id", "repo_id", None, True), ("slug", "slug COLLATE NOCASE", SLUG, False),
                ("state", "state", STATE, False), ("sha", "sha COLLATE NOCASE", SHA, False),
                ("job_number", "json_extract(data,'$.job.number')", None, True),
            ), self.pull)
        if len(path) < 2 or "pull_requests" not in self.tables:
            raise missing()
        identity = positive(path[0]), positive(path[1])
        row = self.db.execute("SELECT * FROM pull_requests WHERE repo_id=? AND number=?", identity).fetchone()
        if row is None:
            raise missing()
        tail = path[2:]
        if not tail:
            query.finish()
            return {"data": self.pull(row)}
        if tail not in (["ci"], ["events"]):
            raise missing()
        sha = query.take("sha", SHA)
        suffix = " AND sha=? COLLATE NOCASE" if sha else ""
        params = (*identity, sha) if sha else identity
        page = query.page()
        if tail == ["ci"] and "pull_ci" in self.tables:
            return page.sql(self.db, "SELECT sha,data FROM pull_ci WHERE repo_id=? AND number=?"
                            + suffix + " ORDER BY sha", params, lambda item: {
                                "repo_id": identity[0], "number": identity[1], "sha": item["sha"],
                                "job_number": public.scalar(public.mapping(decode(row).get("job")).get("number")),
                                **public.snapshot(decode(item)),
                            })
        if tail == ["events"] and "pull_events" in self.tables:
            return page.sql(self.db, "SELECT id,at,sha,kind,detail FROM pull_events WHERE repo_id=? AND number=?"
                            + suffix + " ORDER BY id", params, lambda item: {
                                **public.fields(dict(item), "id at sha kind"),
                                "detail": public.pull_detail(item["kind"], json.loads(item["detail"])),
                            })
        return page.result([], lambda item: item)

    def pull(self, row):
        value = public.pull(decode(row))
        ci_row = None
        if "pull_ci" in self.tables:
            ci_row = self.db.execute("SELECT data FROM pull_ci WHERE repo_id=? AND number=? AND sha=?",
                                     (row["repo_id"], row["number"], row["sha"])).fetchone()
        return value | {
            "source_observation": self.source_errors(row["slug"], row["number"]),
            "ci": public.snapshot(decode(ci_row)) if ci_row else None,
        }


JOB_STAGE = {"planning": "plan", "building": "build", "checking": "checks",
             "reviewing": "review", "publishing": "publish"}


class ObservationHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"error": {"code": "invalid_request", "message": "Solicitud HTTP inválida"}},
                     head=self.command == "HEAD")

    def respond(self, status, payload, *, head=False):
        data = json.dumps({"version": 1, **payload}, ensure_ascii=True, allow_nan=False).encode()
        self.send_content(status, data, "application/json", "default-src 'none'; frame-ancestors 'none'", head)

    def send_content(self, status, data, media_type, policy, head):
        self.send_response(status)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Connection", "close")
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        self.close_connection = True
        if not head:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        head = self.command == "HEAD"
        try:
            host = self.local_host()
            path, _, query = self.path.partition("?")
            if path in DASHBOARD_ASSETS:
                name, media_type = DASHBOARD_ASSETS[path]
                data = files("masf").joinpath("dashboard", name).read_bytes()
                policy = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                          "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
                self.send_content(200, data, media_type, policy, head)
                return
            segments = path.split("/")
            if len(segments) < 3 or segments[:2] != ["", "v1"] or not all(
                IDENT.fullmatch(segment) for segment in segments[2:]
            ):
                raise invalid()
            identity = self.action_route(path)
            if identity and self.server.actions:
                if query:
                    raise invalid()
                self.server.actions.authorize(self.headers, host)
                with closing(connect_readonly(self.server.root)) as db:
                    payload = {"data": self.server.actions.context(db, identity)}
                self.respond(200, payload, head=head)
                return
            with closing(connect_readonly(self.server.root)) as db:
                payload = View(db, self.server.root, self.server.config).dispatch(segments[2:], Query(query))
            if len(segments) == 5 and segments[2] == "jobs":
                payload["data"]["actions_enabled"] = self.server.actions is not None
            self.respond(200, payload, head=head)
        except (RequestError, ActionError) as exc:
            self.respond(exc.status, {"error": {"code": exc.code, "message": exc.message}}, head=head)
        except (sqlite3.Error, ConfigError, OSError, RuntimeError):
            self.respond(503, {"error": {"code": "unavailable", "message": "Datos de observación no disponibles"}},
                         head=head)
        except (ValueError, KeyError, TypeError, RecursionError):
            self.respond(500, {"error": {"code": "invalid_data", "message": "Registro de observación inválido"}},
                         head=head)

    do_HEAD = do_GET

    def local_host(self):
        hosts = self.headers.get_all("Host") or []
        port = self.server.server_port
        if hosts not in (["127.0.0.1"], ["localhost"], [f"127.0.0.1:{port}"], [f"localhost:{port}"]):
            raise RequestError(400, "invalid_host", "El servidor requiere un host local")
        return hosts[0]

    def action_route(self, path):
        match = re.fullmatch(r"/v1/jobs/([0-9]{1,18})/([0-9]{1,18})/actions", path)
        if match:
            return tuple(positive(value) for value in match.groups())
        return None

    def do_POST(self):
        if self.server.actions is None:
            return self.reject_method()
        try:
            host = self.local_host()
            identity = self.action_route(self.path)
            if not identity:
                raise missing()
            self.server.actions.authorize(self.headers, host)
            lengths = self.headers.get_all("Content-Length") or []
            if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                    or not 0 < int(lengths[0]) <= 16384
                    or self.headers.get_content_type() != "application/json"):
                raise invalid()
            keys = self.headers.get_all("Idempotency-Key") or []
            if len(keys) != 1:
                raise invalid()
            try:
                payload = json.loads(self.rfile.read(int(lengths[0])))
            except (ValueError, RecursionError):
                raise invalid() from None
            receipt = self.server.actions.submit(identity, keys[0], payload)
            self.respond(200, {"data": receipt})
        except (RequestError, ActionError) as exc:
            self.respond(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (sqlite3.Error, ConfigError, OSError, RuntimeError, ValueError, KeyError, TypeError):
            self.respond(503, {"error": {"code": "unavailable", "message": "No se pudo confirmar la decisión. Reintenta el mismo envío."}})

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.reject_method
        raise AttributeError(name)

    def reject_method(self):
        self.respond(405, {"error": {"code": "method_not_allowed", "message": "Método de lectura requerido"}})


class ObservationServer(ThreadingHTTPServer):
    def __init__(self, root, config, listen_port, actions_token_file=None):
        self.root, self.config = root, config
        self.actions = Actions(root, config, actions_token_file) if actions_token_file is not None else None
        if self.actions is not None:
            with closing(connect(root)):
                pass
        super().__init__(("127.0.0.1", listen_port), ObservationHandler)


def serve(root, config, listen_port, actions_token_file=None):
    registry(config)
    try:
        with closing(connect_readonly(root)) as db:
            db.execute("SELECT repo_id FROM jobs LIMIT 1")
    except sqlite3.Error as exc:
        raise ConfigError("La cola de observación debe existir y permitir lectura") from exc
    with ObservationServer(root, config, listen_port, actions_token_file) as server:
        print(f"MASF observe-api activo en http://127.0.0.1:{server.server_port}/v1", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
