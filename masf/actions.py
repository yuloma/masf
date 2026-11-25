"""Authenticated operator decisions on the controller's persisted queue."""

import fnmatch
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath

from . import ci, observation_data as public, queue
from .config import load, xdg
from .evidence import digest, timestamp
from .system import command
from .worker import saved_workspace

MAX_ROUNDS = 25
MAX_TIMEOUT = 86400
JOB_STATES = {"needs_input", "blocked"}
RESUME_STATES = {"queued", "planning", "building", "checking", "reviewing", "publishing", "pr_open"}
GIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
KEY = re.compile(r"[a-zA-Z0-9_-]{16,128}")


class ActionError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def invalid(message):
    return ActionError(422, "invalid_decision", message)


def stale(message="El trabajo cambió. Revisa el contexto actualizado."):
    return ActionError(409, "revision_conflict", message)


def exact_path(path):
    if (not isinstance(path, str) or not path or public.scalar(path) != path
            or not path.isprintable() or any(char in path for char in "*?[]\\")
            or path.startswith("/")):
        return False
    parsed = PurePosixPath(path)
    return (bool(parsed.parts) and str(parsed) == path
            and not any(part.lower() in {".", "..", ".git"} for part in parsed.parts))


def resume_state(job):
    if job.get("ci_intervention") and job.get("retry") != "planning":
        return "pr_open"
    return job.get("retry") or ("planning" if job["state"] == "needs_input" else "queued")


def functional_intervention(job):
    diagnostics = (job.get("ci_repair") or {}).get("diagnostics", [])
    return job.get("ci_intervention") and (
        job.get("retry") == "building"
        or (bool(diagnostics) and all(item["kind"] == "functional" for item in diagnostics))
    )


def describe(kind, state, **details):
    return {"kind": kind, "resume_state": state, **details}


def recorded_question(db, job):
    if job.get("plan_attempt"):
        row = db.execute(
            """SELECT data FROM stage_attempts WHERE id=? AND repo_id=? AND number=? AND status='needs_input'""",
            (job["plan_attempt"], job["repo_id"], job["number"]),
        ).fetchone()
        if row:
            return public.scalar(public.mapping(json.loads(row[0]).get("result")).get("question"))
    plan = public.mapping(job.get("plan"))
    return public.scalar(public.mapping(plan.get("result")).get("question") or plan.get("question"))


class Actions:
    def __init__(self, root, config, token_file):
        self.root, self.config = root, config
        with token_file.open("rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("El archivo de acciones debe ser privado (0600)")
            credentials = json.loads(source.read(4097))
        self.operator, token = credentials["operator"], credentials["token"]
        if (not isinstance(self.operator, str) or not self.operator.strip()
                or len(self.operator) > 100 or public.scalar(self.operator) != self.operator
                or not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,256}", token)):
            raise ValueError("Identidad o token de acciones inválido")
        self.authorization = hashlib.sha256(("Bearer " + token).encode()).digest()

    def authorize(self, headers, host):
        origins = headers.get_all("Origin") or []
        if ((origins and origins != ["http://" + host])
                or headers.get("Sec-Fetch-Site") == "cross-site"):
            raise ActionError(403, "invalid_origin", "Origen de la solicitud inválido")
        values = headers.get_all("Authorization") or []
        if len(values) != 1 or not hmac.compare_digest(
            hashlib.sha256(values[0].encode()).digest(), self.authorization
        ):
            raise ActionError(401, "authentication_required", "Token de operador inválido")

    def read_job(self, db, identity):
        row = db.execute("SELECT data FROM jobs WHERE repo_id=? AND number=?", identity).fetchone()
        if row is None:
            raise ActionError(404, "not_found", "Trabajo desconocido")
        job = json.loads(row[0])
        return job, queue.job_revision(db, identity, job)

    def job_dir(self, job):
        jobs = Path(os.environ.get("MASF_JOBS_DIR") or xdg("DATA", ".local/share") / "jobs")
        return jobs / f"{job['repo_id']}-{job['number']}"

    def checkpoints(self, db, job, counts):
        # A directory can be reused and mutated. Only its last recorded identity is selectable.
        records = {}
        for row in db.execute(
            """SELECT json_extract(detail,'$.build_workspace') FROM events WHERE repo_id=? AND number=?
            AND json_type(detail,'$.build_workspace')='object' ORDER BY id""",
            (job["repo_id"], job["number"]),
        ):
            checkpoint = json.loads(row[0])
            records[checkpoint.get("path")] = checkpoint
        current = job.get("build_workspace") or {}
        if current.get("path"):
            records[current["path"]] = current
        failures = {record["id"]: record for record in queue.stage_attempts(db, job, role="build")
                    if record["status"] == "failed"}
        valid = {}
        for path, record in records.items():
            attempt = record.get("failed_attempt")
            if (not path or attempt not in failures or record.get("result")
                    or record.get("input_sha") != (job.get("candidate") or job.get("base"))
                    or record["input_sha"] != failures[attempt].get("input_sha")
                    or record.get("fingerprint") != job["fingerprint"]
                    or type(record.get("round")) is not int
                    or not 0 < record["round"] <= counts["rounds"] <= counts["build_limit"]):
                continue
            try:
                saved_workspace(self.job_dir(job), path)
            except ValueError:
                continue
            valid[digest([job["repo_id"], job["number"], path, attempt])] = record
        return valid

    def describe(self, db, cfg, job):
        repo = next((repo for repo in cfg["repositories"] if repo["enabled"]
                     and repo["slug"] == job["slug"]), None)
        if not repo or job["state"] not in JOB_STATES:
            return {}, {}
        state = resume_state(job)
        if state not in RESUME_STATES:
            return {}, {}
        if job.get("scope_conflict"):
            review = public.mapping(job.get("scope_review"))
            return {"confirm_scope": describe("confirm_scope", "planning", scope={
                key: public.fields(review.get(key), "title body") for key in ("previous", "observed")
            })}, {}
        if job.get("head_conflict"):
            return {}, {}
        counts = ci.limits(job, cfg)
        offers = {}
        copies = {}
        checkpoint = job.get("build_workspace") or {}
        timeout = re.search(r"tiempo agotado \((\d+)s\)", checkpoint.get("error") or job.get("reason") or "")
        paths = public.protected_paths(job.get("reason"))
        if paths and job.get("blocked_paths"):
            paths = job["blocked_paths"]
        if paths and job.get("candidate"):
            if all(exact_path(path) for path in paths):
                offers["allow_paths"] = describe("allow_paths", "checking", paths=paths)
        elif job["state"] == "needs_input" and (question := recorded_question(db, job)):
            offers["answer"] = describe("answer", "planning", question=question)
        elif state == "building" and timeout and job.get("plan"):
            copies = self.checkpoints(db, job, counts)
            if copies and int(timeout[1]) < MAX_TIMEOUT:
                offers["extend_timeout"] = describe(
                    "extend_timeout", "building", previous_seconds=int(timeout[1]),
                    minimum_seconds=int(timeout[1]) + 1, maximum_seconds=MAX_TIMEOUT,
                    copies=[{"reference": reference, "current": copy == checkpoint,
                             **public.fields(copy, "input_sha round failed_attempt")}
                            for reference, copy in copies.items()],
                )
        elif not functional_intervention(job) and not job.get("base_conflict"):
            if state == "building" and checkpoint:
                copies = self.checkpoints(db, job, counts)
            if state == "building" and counts["rounds"] >= counts["build_limit"] and checkpoint not in copies.values():
                return {"add_rounds": describe("add_rounds", state, current_limit=counts["build_limit"],
                                                maximum_extra=MAX_ROUNDS)}, {}
            offers["resolve"] = describe("resolve", state)
            role = {"planning": "plan", "building": "build", "reviewing": "review"}.get(state)
            if role:
                routing = repo["routing"] | (job.get("routing_override") or {})
                providers = [name for name, settings in cfg["providers"].items()
                             if settings["enabled"] and name != routing.get(role)]
                if providers:
                    offers["assign_provider"] = describe(
                        "assign_provider", state, role=role, providers=providers,
                        previous_provider=routing.get(role),
                    )
        if (counts["rounds"] >= counts["build_limit"]
                and (state == "building" or functional_intervention(job))):
            offers["add_rounds"] = describe("add_rounds", state, current_limit=counts["build_limit"],
                                           maximum_extra=MAX_ROUNDS)
        return offers, copies

    def context(self, db, identity):
        job, revision = self.read_job(db, identity)
        offers, copies = self.describe(db, load(self.config), job)
        if "allow_paths" in offers:
            offer = offers["allow_paths"]
            offer["changes"] = self.changed_paths(job, offer["paths"])
        return {"revision": revision, "operator": self.operator, "actions": list(offers.values()),
                "compatible_build_copies": len(copies)}

    def submit(self, identity, key, payload):
        if not KEY.fullmatch(key or "") or not isinstance(payload, dict):
            raise invalid("Indica una clave de idempotencia y una decisión válidas.")
        if set(payload) != {"kind", "revision", "evidence", "confirm", "options"} or (
            not isinstance(payload["kind"], str) or not isinstance(payload["revision"], str)
            or not isinstance(payload["evidence"], str) or not payload["evidence"].strip()
            or len(payload["evidence"]) > 4000 or payload["confirm"] is not True
            or not isinstance(payload["options"], dict)
        ):
            raise invalid("Indica el motivo, la confirmación y la revisión del trabajo.")
        signature = digest([self.operator, identity, payload])
        with closing(queue.connect(self.root)) as db:
            receipt = self.duplicate(db, key, signature)
            if receipt is not None:
                return receipt
            token = uuid.uuid4().hex
            job, _ = self.read_job(db, identity)
            # Holds against controller workers, CLI resumption and other HTTP requests.
            if not queue.claim(db, job, token, 600):
                receipt = self.duplicate(db, key, signature)
                if receipt is not None:
                    return receipt
                raise ActionError(409, "decision_in_progress", "Hay otra operación en curso. Reintenta el mismo envío.")
            try:
                receipt = self.duplicate(db, key, signature)
                if receipt is not None:
                    return receipt
                job, revision = self.read_job(db, identity)
                if revision != payload["revision"]:
                    raise stale()
                job["attempt"] = token
                cfg = load(self.config)
                offers, copies = self.describe(db, cfg, job)
                offer = offers.get(payload["kind"])
                if not offer:
                    raise invalid("La acción no está permitida para este trabajo.")
                self.validate_options(offer, payload["options"])
                receipt = self.apply(db, cfg, job, offer, copies, payload)
                updates = receipt.pop("updates")
                try:
                    queue.save(db, job, receipt["state"],
                               action_receipt=(key, signature, receipt, revision), **updates)
                except queue.RevisionConflict:
                    raise stale() from None
                except sqlite3.IntegrityError:
                    duplicate = self.duplicate(db, key, signature)
                    if duplicate is None:
                        raise
                    return duplicate
                return receipt
            finally:
                with db:
                    db.execute("DELETE FROM leases WHERE token=?", (token,))

    def duplicate(self, db, key, signature):
        row = db.execute("SELECT signature,data FROM action_receipts WHERE key=?", (key,)).fetchone()
        if row is not None:
            if row["signature"] != signature:
                raise ActionError(409, "idempotency_conflict", "La clave ya corresponde a otra decisión.")
            return json.loads(row["data"])
        return None

    def validate_options(self, offer, options):
        match offer["kind"]:
            case "allow_paths":
                valid = options == {"paths": offer["paths"]}
            case "assign_provider":
                valid = set(options) == {"provider"} and options["provider"] in offer["providers"]
            case "add_rounds":
                valid = (set(options) == {"extra"} and type(options["extra"]) is int
                         and 1 <= options["extra"] <= offer["maximum_extra"])
            case "extend_timeout":
                valid = (set(options) == {"seconds", "reference"} and type(options["seconds"]) is int
                         and offer["minimum_seconds"] <= options["seconds"] <= offer["maximum_seconds"]
                         and any(copy["reference"] == options["reference"] for copy in offer["copies"]))
            case _:
                valid = not options
        if not valid:
            raise invalid("Los valores elegidos no coinciden con las opciones del trabajo.")

    def apply(self, db, cfg, job, offer, copies, payload):
        kind, state = offer["kind"], offer["resume_state"]
        options = payload["options"]
        accepted = timestamp()
        repo = next(repo for repo in cfg["repositories"] if repo["slug"] == job["slug"])
        issue = queue.api(f"repos/{job['slug']}/issues/{job['number']}")
        if not queue.eligible(issue, repo) or not queue.eligible(issue, job["repo"]):
            raise stale("La issue ya no está habilitada para este trabajo.")
        scope = queue.fingerprint(issue)
        expected_scope = job.get("scope_conflict") if kind == "confirm_scope" else job["fingerprint"]
        if scope != expected_scope:
            queue.save(db, job, "needs_input", retry="planning", scope_conflict=scope,
                       reason="El contenido de la issue cambió. Revisa y confirma el alcance actualizado.",
                       scope_review=ci.scope_review(job, issue))
            raise stale()
        if job.get("pr_number"):
            pr = ci.pull(job, queue.api)
            if not ci.validate(db, job, pr, {ci.expected_head(job)}):
                raise stale("Cambió el estado o la procedencia del PR. Revisa el contexto.")
            if kind in {"allow_paths", "add_rounds", "extend_timeout"}:
                remote = queue.api(f"repos/{job['slug']}/git/ref/heads/{ci.branch(job)}")["object"]["sha"]
                if remote != ci.expected_head(job):
                    ci.head_conflict(db, job, pr["head"]["sha"], remote=remote)
                    raise stale("La referencia del PR cambió. Revisa el contexto.")
        entry = dict(id=uuid.uuid4().hex, kind="rounds" if kind == "add_rounds" else kind,
                     operator=self.operator, at=accepted, evidence=ci.excerpt(payload["evidence"]),
                     reason=job.get("reason"), fingerprint=scope, candidate=job.get("candidate"),
                     pr=job.get("pr_number"), resume_state=state)
        updates = {"reason": "", "ci_retry_at": 0}
        match kind:
            case "confirm_scope":
                updates.update(title=issue["title"], body=issue["body"], fingerprint=scope,
                               scope_conflict=None, scope_review=None, rounds=0, recovery_builds=0, ci_rounds=0,
                               plan=None, plan_attempt=None, review={}, checks={},
                               candidate=ci.expected_head(job) if job.get("pr_number") else None,
                               build_workspace=None, build_recovery=None, head_conflict=None,
                               ci_handled=[], ci_repair=None, ci_intervention=False, repair_intent=None)
                entry["previous_fingerprint"] = job["fingerprint"]
            case "answer":
                updates.update(plan=None, plan_attempt=None)
            case "allow_paths":
                self.changed_paths(job, offer["paths"])
                approvals = {path: {"fingerprint": scope, "candidate": job["candidate"],
                                    "at": accepted, "operator": self.operator, "evidence": entry["evidence"]}
                             for path in offer["paths"]}
                updates.update(path_approvals=(job.get("path_approvals") or {}) | approvals,
                               blocked_paths=None)
                entry["paths"] = offer["paths"]
            case "assign_provider":
                role, provider = offer["role"], options["provider"]
                overrides = (job.get("routing_override") or {}) | {role: provider}
                updates.update(routing_override=overrides, repo=job["repo"] | {
                    "routing": repo["routing"] | overrides,
                })
                entry.update(role=role, provider=provider, previous_provider=offer["previous_provider"])
            case "add_rounds":
                entry.update(rounds=options["extra"], round_limit=offer["current_limit"] + options["extra"])
                repair = job.get("ci_repair") or {}
                if state == "pr_open" and repair:
                    updates.update(ci_handled=[item for item in job.get("ci_handled", [])
                                               if (item["sha"], item["key"]) != (repair["sha"], repair["key"])])
            case "extend_timeout":
                copy = copies[options["reference"]]
                updates.update(build_workspace=copy | {"operator_resume": True},
                               build_recovery=None,
                               stage_timeouts=(job.get("stage_timeouts") or {}) | {"build": options["seconds"]})
                entry.update(timeout_seconds=options["seconds"], workspace=options["reference"],
                             previous_timeout_seconds=offer["previous_seconds"])
        if state == "pr_open":
            updates.update(ci_repair=None, ci_intervention=False)
        if state == "building" and kind in {"resolve", "assign_provider", "add_rounds"}:
            current = job.get("build_workspace")
            copy = current | {"operator_resume": True} if current in copies.values() else None
            updates.update(build_workspace=copy, build_recovery=None)
        updates["operator_feedback"] = {"source": "operator", "instruction": entry["evidence"],
                                        "type": kind, "sha": entry["candidate"], "question": offer.get("question")}
        updates["decisions"] = [*job.get("decisions", []), entry]
        return {"id": entry["id"], "operator": self.operator, "kind": kind, "status": "applied",
                "accepted_at": accepted, "applied_at": timestamp(), "state": state,
                "updates": updates}

    def changed_paths(self, job, paths):
        base, candidate = job.get("base"), job.get("candidate")
        if not all(isinstance(sha, str) and GIT_SHA.fullmatch(sha) for sha in (base, candidate)):
            raise invalid("El trabajo necesita SHAs completos para autorizar las rutas.")
        store = self.job_dir(job) / "git"
        stats = command(["git", "--literal-pathspecs", "--git-dir", str(store), "diff", "--no-ext-diff",
                         "--no-textconv", "--no-renames", "--numstat", "-z", base, candidate, "--", *paths],
                        timeout=10)
        changes = {}
        for line in stats.split("\0"):
            parts = line.split("\t", 2)
            if len(parts) == 3:
                added, removed, path = parts
                changes[path] = {"path": path, "added": int(added) if added.isdecimal() else None,
                                 "removed": int(removed) if removed.isdecimal() else None}
        if not all(path in changes and any(fnmatch.fnmatchcase(path, pattern)
                                           for pattern in job["repo"].get("protected_paths", []))
                   for path in paths):
            raise stale("Las rutas ya no coinciden con los cambios protegidos del candidato.")
        return [changes[path] for path in paths]
