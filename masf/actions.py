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
from urllib.parse import quote

from . import ci, observation_data as public, queue, recovery
from .config import load, xdg
from .evidence import digest, timestamp
from .system import ProcessError, command
from .worker import Worker, saved_workspace

MAX_ROUNDS = 25
MAX_TIMEOUT = 86400
JOB_STATES = {"needs_input", "blocked"}
RESUME_STATES = {"queued", "planning", "building", "checking", "reviewing", "publishing", "pr_open"}
GIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
KEY = re.compile(r"[a-zA-Z0-9_-]{16,128}")


class ActionError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
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
    if job.get("ci_intervention") and not job.get("pr_conflict") and job.get("retry") != "planning":
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
            text = public.scalar(public.mapping(json.loads(row[0]).get("result")).get("question"))
            if text:
                return {"text": text, "attempt_id": job["plan_attempt"]}
    plan = public.mapping(job.get("plan"))
    text = public.scalar(public.mapping(plan.get("result")).get("question") or plan.get("question"))
    return {"text": text, "attempt_id": job.get("plan_attempt")} if text else None


class Decisions:
    def __init__(self, root, cfg, operator, origin, lookup=None, latest=None):
        self.root, self.cfg, self.operator, self.origin = root, cfg, operator, origin
        self.lookup = lookup or queue.api
        self.latest = latest or (lambda slug, branch: self.lookup(
            f"repos/{slug}/commits/{quote(branch, safe='')}")["sha"])

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
                    or not 0 < record["round"] <= min(counts["rounds"], counts["build_limit"])):
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
        counts = ci.limits(job, cfg)
        if job.get("head_conflict"):
            return {"accept_head": describe("accept_head", "pr_open",
                                            head=job["head_conflict"]["observed"],
                                            current_limit=counts["build_limit"],
                                            maximum_extra=MAX_ROUNDS
                                            if counts["rounds"] >= counts["build_limit"] else 0)}, {}
        offers = {}
        copies = {}
        checkpoint = job.get("build_workspace") or {}
        timeout = re.search(r"tiempo agotado \((\d+)s\)", checkpoint.get("error") or job.get("reason") or "")
        paths = public.protected_paths(job.get("reason"))
        if job["state"] == "blocked" and job.get("candidate") and (
            paths or (job.get("reason") or "").startswith("Ruta protegida")
        ):
            paths = job.get("blocked_paths") or [entry["path"] for entry in self.changed_paths(job)]
            if paths and all(exact_path(path) for path in paths):
                return {"allow_paths": describe("allow_paths", "checking", paths=paths)}, {}
            return {}, {}
        elif job["state"] == "needs_input" and (question := recorded_question(db, job)):
            return {"answer": describe("answer", "planning", question=question["text"],
                                       question_attempt=question["attempt_id"])}, {}
        elif state == "building" and timeout and job.get("plan"):
            copies = self.checkpoints(db, job, counts)
            if copies and int(timeout[1]) < MAX_TIMEOUT:
                offers["extend_timeout"] = describe(
                    "extend_timeout", "building", previous_seconds=int(timeout[1]),
                    minimum_seconds=int(timeout[1]) + 1, maximum_seconds=MAX_TIMEOUT,
                    copies=self.copy_choices(copies, checkpoint),
                )
        elif (not functional_intervention(job) or state == "planning") and not self.base_conflict(job):
            if state == "building" and checkpoint:
                copies = self.checkpoints(db, job, counts)
            if state != "building" or counts["rounds"] < counts["build_limit"] or checkpoint in copies.values():
                offers["resolve"] = describe("resolve", state)
                if copies:
                    offers["reuse_workspace"] = describe("reuse_workspace", "building",
                                                         copies=self.copy_choices(copies, checkpoint))
                routing = repo.get("routing", {}) | (job.get("routing_override") or {})
                providers = {role: [name for name, settings in cfg.get("providers", {}).items()
                                    if settings["enabled"] and name != current]
                             for role, current in routing.items()}
                providers = {role: choices for role, choices in providers.items() if choices}
                if providers:
                    role = {"planning": "plan", "reviewing": "review"}.get(state, "build")
                    role = role if role in providers else next(iter(providers))
                    offers["assign_provider"] = describe(
                        "assign_provider", state, role=role, providers=providers[role],
                        previous_provider=routing[role], roles=providers, routing=routing,
                    )
        if job["state"] == "blocked" and (conflict := self.base_conflict(job)):
            resolutions = self.resolutions(job, conflict)
            if resolutions:
                offers["resolved_commit"] = describe("resolved_commit", "checking", commits=resolutions)
            if not self.recovery_exhausted(job):
                offers["resolve"] = describe("resolve", "publishing" if not job.get("pr_number") else "checking")
        if self.recovery_exhausted(job):
            offers.pop("resolve", None)
            offers["add_recoveries"] = describe("add_recoveries", state,
                                                current_limit=recovery.limit(job, cfg), maximum_extra=MAX_ROUNDS)
        if (counts["rounds"] >= counts["build_limit"]
                and state != "planning" and (state == "building" or functional_intervention(job))):
            offers["add_rounds"] = describe("add_rounds", state, current_limit=counts["build_limit"],
                                           maximum_extra=MAX_ROUNDS)
        return offers, copies

    def copy_choices(self, copies, checkpoint):
        return [{"reference": reference, "current": copy == checkpoint,
                 **public.fields(copy, "input_sha round failed_attempt")}
                for reference, copy in copies.items()]

    def recovery_exhausted(self, job):
        return (job["state"] == "blocked"
                and len(job.get("recoveries", [])) >= recovery.limit(job, self.cfg)
                and self.pending_recovery(job))

    def pending_recovery(self, job):
        if job.get("recovery_exhausted"):
            return job["recovery_exhausted"]
        reason = job.get("reason") or ""
        if reason.startswith("Recuperaciones automáticas agotadas"):
            return "reserve"
        history = job.get("recoveries") or []
        if history and reason.endswith(recovery.EXHAUSTION_NOTE):
            latest = history[-1]
            if (latest.get("action") == "retry" and latest.get("stage") == job.get("retry")
                    and latest.get("reason") == reason.removesuffix(recovery.EXHAUSTION_NOTE)):
                return "retry"
        return None

    def base_conflict(self, job):
        conflict = job.get("base_conflict")
        if not conflict or (job.get("pr_conflict") or {}).get("status") == "resolved":
            return None
        return conflict

    def resolution_prefix(self, job, conflict):
        return f"refs/masf/resolutions/{job['fingerprint']}/{conflict['base']}/"

    def resolutions(self, job, conflict):
        if (conflict.get("observed_base", conflict["base"]) != conflict["base"]
                or conflict.get("fingerprint", job["fingerprint"]) != job["fingerprint"]):
            return []
        store = self.job_dir(job) / "git"
        if not store.is_dir() or store.is_symlink() or store.parent.is_symlink():
            return []
        git = ["git", "--no-replace-objects", "--git-dir", str(store)]
        prefix = self.resolution_prefix(job, conflict)
        refs = command(git + ["for-each-ref", "--format=%(refname) %(objectname) %(objecttype)",
                              prefix], timeout=10)
        commits = []
        for ref, sha, kind in (line.split() for line in refs.splitlines()):
            if kind != "commit" or not GIT_SHA.fullmatch(sha) or ref != prefix + sha:
                continue
            try:
                for ancestor in (conflict["candidate"], conflict["base"]):
                    command(git + ["merge-base", "--is-ancestor", ancestor, sha], timeout=10)
            except ProcessError as exc:
                if exc.returncode == 1:
                    continue
                raise
            commits.append(sha)
        return commits

    def context(self, db, identity):
        job, revision = self.read_job(db, identity)
        offers, copies = self.describe(db, self.cfg, job)
        if "allow_paths" in offers:
            offer = offers["allow_paths"]
            offer["changes"] = self.changed_paths(job, offer["paths"])
        conflict = self.base_conflict(job) if job["state"] == "blocked" else None
        return {"revision": revision, "operator": self.operator, "actions": list(offers.values()),
                "base_conflict": public.conflict(conflict),
                "resolution_ref_prefix": self.resolution_prefix(job, conflict) if conflict else None,
                "compatible_build_copies": len(copies)}

    def submit(self, db, identity, key, payload):
        if not KEY.fullmatch(key or "") or not isinstance(payload, dict):
            raise invalid("Indica una clave de idempotencia y una decisión válidas.")
        if set(payload) != {"kind", "revision", "evidence", "confirm", "options"} or (
            not isinstance(payload["kind"], str) or not isinstance(payload["revision"], str)
            or not isinstance(payload["evidence"], str) or not payload["evidence"].strip()
            or len(payload["evidence"]) > 4000 or payload["confirm"] is not True
            or not isinstance(payload["options"], dict)
        ):
            raise invalid("Indica el motivo, la confirmación y la revisión del trabajo.")
        if payload["kind"] == "allow_paths":
            paths = payload["options"].get("paths")
            if not isinstance(paths, list) or not paths or not all(exact_path(path) for path in paths):
                raise invalid("Indica una ruta protegida exacta relativa al repositorio.")
        signature = digest([self.operator, identity, payload])
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
            if self.read_job(db, identity)[1] != payload["revision"]:
                raise stale()
            raise ActionError(409, "decision_in_progress", "Hay otra operación en curso. Reintenta el mismo envío.")
        try:
            receipt = self.duplicate(db, key, signature)
            if receipt is not None:
                return receipt
            job, revision = self.read_job(db, identity)
            if revision != payload["revision"]:
                raise stale()
            job["attempt"] = token
            cfg = self.cfg
            offers, copies = self.describe(db, cfg, job)
            offer = offers.get(payload["kind"])
            if not offer:
                if payload["kind"] == "resolved_commit":
                    raise invalid("Selecciona un commit conservado por el trabajo con ambos ancestros vigentes.")
                raise invalid("La acción no está permitida para este trabajo.")
            self.validate_options(offer, payload["options"], copies)
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

    def validate_options(self, offer, options, copies):
        match offer["kind"]:
            case "allow_paths":
                valid = (set(options) == {"paths"} and len(options["paths"]) == len(offer["paths"])
                         and set(options["paths"]) == set(offer["paths"]))
            case "assign_provider":
                role = options.get("role", offer["role"])
                valid = (set(options) in ({"provider"}, {"role", "provider"}) and isinstance(role, str)
                         and options["provider"] in offer["roles"].get(role, ()))
            case "add_rounds" | "add_recoveries":
                valid = (set(options) == {"extra"} and type(options["extra"]) is int
                         and 1 <= options["extra"] <= offer["maximum_extra"])
            case "extend_timeout":
                valid = (set(options) == {"seconds", "reference"} and type(options["seconds"]) is int
                         and offer["minimum_seconds"] <= options["seconds"] <= offer["maximum_seconds"]
                         and any(copy["reference"] == options["reference"] for copy in offer["copies"]))
            case "reuse_workspace":
                valid = (set(options) == {"reference"} and any(
                             copy["reference"] == options["reference"] for copy in offer["copies"])
                         or self.origin == "cli" and set(options) == {"path"}
                         and any(copy["path"] == options["path"] for copy in copies.values()))
            case "resolved_commit":
                valid = set(options) == {"sha"} and options["sha"] in offer["commits"]
            case "accept_head":
                valid = (set(options) in ({"sha"}, {"sha", "extra"}) and options["sha"] == offer["head"]
                         and ("extra" not in options or (type(options["extra"]) is int
                              and 1 <= options["extra"] <= offer["maximum_extra"])))
            case _:
                valid = not options
        if not valid:
            if offer["kind"] == "resolved_commit":
                raise invalid("Selecciona un commit conservado por el trabajo con ambos ancestros vigentes.")
            raise invalid("Los valores elegidos no coinciden con las opciones del trabajo.")

    def apply(self, db, cfg, job, offer, copies, payload):
        kind, state = offer["kind"], offer["resume_state"]
        options = payload["options"]
        accepted = timestamp()
        repo = next(repo for repo in cfg["repositories"] if repo["slug"] == job["slug"])
        issue = self.lookup(f"repos/{job['slug']}/issues/{job['number']}")
        if not queue.eligible(issue, repo) or not queue.eligible(issue, job["repo"]):
            raise stale("La issue ya no está habilitada para este trabajo.")
        scope = queue.fingerprint(issue)
        expected_scope = job.get("scope_conflict") if kind == "confirm_scope" else job["fingerprint"]
        if scope != expected_scope:
            queue.save(db, job, "needs_input", retry="planning", scope_conflict=scope,
                       reason="El contenido de la issue cambió. Revisa y confirma el alcance actualizado.",
                       scope_review=ci.scope_review(job, issue))
            raise stale()
        pr = None
        if job.get("pr_number"):
            pr = ci.pull(job, self.lookup)
            head = options["sha"] if kind == "accept_head" else ci.expected_head(job)
            intent = (job.get("repair_intent") or {}).get("target") if kind == "resolve" else None
            heads = {head, intent} if intent else {head}
            if not ci.validate(db, job, pr, heads):
                raise stale("Cambió el estado o la procedencia del PR. Revisa el contexto.")
            if kind in {"allow_paths", "add_rounds", "extend_timeout", "reuse_workspace",
                        "accept_head", "add_recoveries", "resolved_commit"}:
                remote = self.lookup(f"repos/{job['slug']}/git/ref/heads/{ci.branch(job)}")["object"]["sha"]
                if remote != head:
                    ci.head_conflict(db, job, pr["head"]["sha"], remote=remote)
                    raise stale("La referencia del PR cambió. Revisa el contexto.")
        entry = dict(id=uuid.uuid4().hex, kind="rounds" if kind == "add_rounds" else kind,
                     operator=self.operator, at=accepted, evidence=ci.excerpt(payload["evidence"], limit=4000),
                     reason=job.get("reason"), fingerprint=scope, candidate=job.get("candidate"),
                     pr=job.get("pr_number"), resume_state=state, origin=self.origin,
                     expected_revision=payload["revision"], result="applied")
        updates = {"reason": "", "ci_retry_at": 0, "retry": None, "recovery_wait": {}}
        additions = [entry]
        overrides = (job.get("routing_override") or {}).copy()
        match kind:
            case "confirm_scope":
                updates.update(title=issue["title"], body=issue["body"], fingerprint=scope,
                               scope_conflict=None, scope_review=None, rounds=0, recovery_builds=0, ci_rounds=0,
                               plan=None, plan_attempt=None, review={}, checks={}, base_checked=None,
                               path_approvals={}, blocked_paths=None, feedback=None,
                               candidate=ci.expected_head(job) if job.get("pr_number") else None,
                               build_workspace=None, build_recovery=None, head_conflict=None,
                               ci_handled=[], ci_repair=None, ci_intervention=False, repair_intent=None,
                               previous_intent=job.get("intent"), intent=None, integration_approval=None,
                               base_conflict=None)
                updates.update(ci.restart_conflict(job))
                entry["previous_fingerprint"] = job["fingerprint"]
            case "answer":
                updates.update(plan=None, plan_attempt=None)
                entry.update(question=offer["question"], question_attempt=offer["question_attempt"],
                             answer=entry["evidence"])
            case "allow_paths":
                self.changed_paths(job, offer["paths"])
                approvals = {path: {"fingerprint": scope, "candidate": job["candidate"],
                                    "at": accepted, "operator": self.operator, "evidence": entry["evidence"]}
                             for path in offer["paths"]}
                updates.update(path_approvals=(job.get("path_approvals") or {}) | approvals,
                               blocked_paths=None)
                entry["paths"] = offer["paths"]
            case "assign_provider":
                role, provider = options.get("role", offer["role"]), options["provider"]
                overrides[role] = provider
                updates["routing_override"] = overrides
                entry.update(role=role, provider=provider, previous_provider=offer["routing"][role])
            case "add_rounds":
                entry.update(rounds=options["extra"], round_limit=offer["current_limit"] + options["extra"])
                repair = job.get("ci_repair") or {}
                if state == "pr_open" and repair:
                    updates.update(ci_handled=[item for item in job.get("ci_handled", [])
                                               if (item["sha"], item["key"]) != (repair["sha"], repair["key"])])
            case "extend_timeout" | "reuse_workspace":
                reference = options.get("reference")
                if reference is None:
                    reference = next(ref for ref, copy in copies.items() if copy["path"] == options["path"])
                copy = copies[reference]
                updates["build_workspace"] = copy | {"operator_resume": True}
                entry["workspace"] = reference
                if not job.get("pr_conflict"):
                    updates["build_recovery"] = None
                if kind == "extend_timeout":
                    updates["stage_timeouts"] = (job.get("stage_timeouts") or {}) | {"build": options["seconds"]}
                    entry.update(timeout_seconds=options["seconds"],
                                 previous_timeout_seconds=offer["previous_seconds"])
            case "accept_head":
                head = options["sha"]
                entry.update(kind="head", head=head, previous=ci.expected_head(job), conflict=job["head_conflict"])
                if "extra" in options:
                    additions.append(entry | {"id": uuid.uuid4().hex, "kind": "rounds", "rounds": options["extra"],
                                              "round_limit": offer["current_limit"] + options["extra"]})
                updates.update(published_head=head, candidate=head, head_conflict=None, repair_intent=None,
                               checks={}, review={}, build_workspace=None, pr_conflict=None, base_conflict=None,
                               build_recovery=None, integration_approval=None,
                               ci={"sha": head, "state": "waiting", "checks": [], "pending_runs": []})
            case "resolved_commit":
                self.apply_resolution(db, cfg, job, pr, entry, updates, options["sha"])
            case "add_recoveries":
                limit = offer["current_limit"] + options["extra"]
                updates.update(recovery_limit=limit, recovery_exhausted=False)
                entry.update(recoveries=options["extra"], recovery_limit=limit)
                if self.pending_recovery(job) == "retry":
                    history = recovery.reserve(job | {"recovery_limit": limit}, cfg, "retry",
                                               stage=state, reason=(job.get("reason") or "").removesuffix(
                                                   recovery.EXHAUSTION_NOTE),
                                               candidate=job.get("candidate"))
                    updates["recoveries"] = history
                    if state == "building":
                        updates["build_recovery"] = (job.get("build_recovery") or {"type": "retry"}) | {
                            "started": False,
                        }
            case "resolve":
                if self.base_conflict(job) and job.get("pr_conflict"):
                    updates.update(ci.restart_conflict(job))
                if state == "planning" and job.get("ci_intervention"):
                    repair = job.get("ci_repair") or {}
                    updates.update(ci_repair=None, ci_intervention=False, ci_handled=[], repair_intent=None)
                    if repair.get("diagnostics"):
                        updates["feedback"] = {"source": "CI", "sha": repair["sha"],
                                               "diagnostics": repair["diagnostics"]}
        routing = repo.get("routing", job["repo"].get("routing", {})) | overrides
        if routing != job["repo"].get("routing", {}):
            updates.update(repo=job["repo"] | {"routing": routing},
                           routing_history=[*job.get("routing_history", []), job["repo"].get("routing", {})])
            if routing.get("review") != job["repo"].get("routing", {}).get("review"):
                updates["review"] = {}
        if state == "pr_open":
            updates.update(ci_repair=None, ci_intervention=False)
        if state == "building" and kind in {"resolve", "assign_provider", "add_rounds"}:
            current = job.get("build_workspace")
            copy = current | {"operator_resume": True} if current in copies.values() else None
            updates.update(build_workspace=copy, build_recovery=job.get("build_recovery")
                           if job.get("pr_conflict") else None)
        updates["operator_feedback"] = {"source": "operator", "instruction": entry["evidence"],
                                        "type": kind, "sha": entry["candidate"], "question": entry.get("question"),
                                        "question_attempt": entry.get("question_attempt"), "answer": entry.get("answer")}
        updates["decisions"] = [*job.get("decisions", []), *additions]
        return {"id": entry["id"], "operator": self.operator, "kind": kind, "status": "applied",
                "accepted_at": accepted, "applied_at": timestamp(), "state": state,
                "revision": payload["revision"], "origin": self.origin,
                "updates": updates}

    def apply_resolution(self, db, cfg, job, pr, entry, updates, sha):
        conflict = self.base_conflict(job)
        base = self.latest(job["slug"], job["branch"])
        if base != conflict["base"]:
            queue.save(db, job, job["state"], base_conflict=conflict | {"observed_base": base})
            raise stale("La rama base avanzó. Revisa el conflicto actualizado.")
        previous = conflict["candidate"]
        pull = job.get("pr_conflict")
        if (previous != (ci.expected_head(job) if pull else job.get("candidate"))
                or (pull and (pull["fingerprint"] != job["fingerprint"]
                              or pr["base"].get("sha") != base))):
            raise stale("Cambió el alcance o la referencia del conflicto.")
        resolver = Worker(db, cfg, None, self.root, self.job_dir(job).parent, job)
        try:
            ref = self.resolution_prefix(job, conflict) + sha
            current = resolver.git("show-ref", "--hash", "--verify", ref)
        except ProcessError:
            raise stale("Cambió la referencia local de resolución. Revisa el contexto.") from None
        if current != sha:
            raise stale("Cambió la referencia local de resolución. Revisa el contexto.")
        try:
            resolver.check_resolution(sha, base, previous, conflict["paths"])
        except recovery.RecoverableFailure as exc:
            raise invalid(str(exc)) from None
        revisions = {"previous_base": job["base"], "previous_candidate": previous,
                     "base": base, "candidate": sha, "at": entry["at"]}
        entry.update(revisions)
        updates.update(base=base, candidate=sha, checks={}, review={}, base_checked=None, base_conflict=None,
                       build_workspace=None, build_recovery=None, repair_intent=None, integration_approval=None,
                       recovery_exhausted=False, base_resolutions=[*job.get("base_resolutions", []), revisions])
        if pull:
            updates["pr_conflict"] = pull | {
                "base": base, "head": previous, "paths": conflict["paths"], "status": "resolved",
                "candidate": sha, "decision": "Resolución autorizada por el operador",
            }

    def changed_paths(self, job, paths=None):
        base, candidate = job.get("base"), job.get("candidate")
        if not all(isinstance(sha, str) and GIT_SHA.fullmatch(sha) for sha in (base, candidate)):
            raise invalid("El trabajo necesita SHAs completos para autorizar las rutas.")
        store = self.job_dir(job) / "git"
        stats = command(["git", "--no-replace-objects", "--literal-pathspecs", "--git-dir", str(store),
                         "diff", "--no-ext-diff",
                         "--no-textconv", "--no-renames", "--numstat", "-z", base, candidate, "--", *(paths or ())],
                        timeout=10)
        changes = {}
        for line in stats.split("\0"):
            parts = line.split("\t", 2)
            if len(parts) == 3:
                added, removed, path = parts
                changes[path] = {"path": path, "added": int(added) if added.isdecimal() else None,
                                 "removed": int(removed) if removed.isdecimal() else None}
        changes = {path: info for path, info in changes.items() if exact_path(path)
                   and any(fnmatch.fnmatchcase(path, pattern)
                           for pattern in job["repo"].get("protected_paths", []))}
        if paths and not all(path in changes for path in paths):
            raise stale("Las rutas ya no coinciden con los cambios protegidos del candidato.")
        return [changes[path] for path in (paths or changes)]


class Actions:
    def __init__(self, root, config, token_file):
        self.root, self.config = root, config
        with token_file.open("rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("El archivo de acciones debe ser privado (0600)")
            credentials = json.loads(source.read(4097))
        operator, token = credentials["operator"], credentials["token"]
        if (not isinstance(operator, str) or not operator.strip()
                or len(operator) > 100 or public.scalar(operator) != operator
                or not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,256}", token)):
            raise ValueError("Identidad o token de acciones inválido")
        self.operator = operator
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

    def context(self, db, identity):
        return Decisions(self.root, load(self.config), self.operator, "api").context(db, identity)

    def submit(self, identity, key, payload):
        with closing(queue.connect(self.root)) as db:
            return Decisions(self.root, load(self.config), self.operator, "api").submit(db, identity, key, payload)
