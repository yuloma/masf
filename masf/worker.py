from contextlib import contextmanager
import fnmatch
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from . import checks, ci, providers
from .evidence import agent_explanation, explanation, model, provenance, timestamp, usage, write_artifact
from .queue import api, interrupt_stages, latest, record_stage, require_lease, retry_delay, save, stage_attempts
from .recovery import BudgetExhausted, RecoverableFailure, limit, reserve, wait
from .system import ProcessError, TaskStopped, clean, command, docker_env, filtered


def saved_workspace(root, path):
    folder = Path(path).absolute()
    expected = root.resolve()
    if (
        folder.resolve() != folder
        or folder.parent != expected
        or folder.name == "git"
        or not folder.is_dir()
    ):
        raise ValueError(
            "La copia recuperada debe ser un directorio directo del trabajo"
        )
    return folder


class ProtectedPaths(RuntimeError):
    def __init__(self, paths):
        super().__init__("Ruta protegida: " + paths[0])
        self.paths = paths


class Worker:
    def __init__(self, db, cfg, config, state, jobs, job):
        self.db, self.cfg, self.config, self.job = db, cfg, config, job
        ident = f"{job['repo_id']}-{job['number']}"
        self.root = jobs / ident
        self.evidence = state / "evidence" / ident
        for folder in (self.root, self.evidence):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = self.root / "git"
        self.url = f"https://github.com/{job['slug']}.git"
        self.next_admission = 0
        self.owner = job.setdefault("owner", uuid.uuid4().hex)

    def update(self, state, **fields):
        save(self.db, self.job, state, **fields)

    def guard(self):
        require_lease(self.db, self.job)
        if time.monotonic() >= self.next_admission:
            self.next_admission = time.monotonic() + 5
            if not self.admission() or (self.job.get("pr_number") and not self.pr_head()):
                raise TaskStopped("La tarea dejó de ser elegible")

    def git(self, *args, workspace=None, stdin=None, guard=None):
        prefix = [
            "git",
            "--no-replace-objects",
            "-c",
            "credential.helper=!gh auth git-credential",
            "-c",
            "user.name=MASF",
            "-c",
            "user.email=masf@users.noreply.github.com",
            "-c",
            "core.hooksPath=/dev/null",
            "--git-dir",
            str(self.store),
        ]
        if workspace:
            prefix += ["--work-tree", str(workspace)]
        options = {"guard": guard} if guard else {}
        return command(prefix + list(args), cwd=workspace or self.root, stdin=stdin, **options)

    def checkout(self, sha):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        self.git("read-tree", sha)
        self.git("checkout-index", "--all", workspace=folder)
        return folder

    @contextmanager
    def stage(self, role, sha, *, provider=None, commands=(), evidence_files=(), settings=None):
        job = self.job
        settings = self.cfg if settings is None else settings
        prior = stage_attempts(self.db, job, role=role) if self.db is not None else []
        previous = prior[-1] if prior else {}
        ident = uuid.uuid4().hex
        file = self.evidence / f"{role}-{ident}.json"
        refs = [file.name, *evidence_files]
        if role in {"build", "review"}:
            refs.append(job.get("plan_attempt"))
        if role in {"review", "build"}:
            refs.append((job.get("checks") or {}).get("stage_attempt"))
        if role == "build":
            refs.append((job.get("review") or {}).get("stage_attempt"))
        repair = job.get("ci_repair") or {}
        record = {
            "version": 1, "id": ident, "repo_id": job["repo_id"], "number": job["number"],
            "role": role, "provider": provider, "executor": provider or "masf",
            "execution_id": None, "cli_version": None, "lease": job.get("attempt"),
            "input_sha": sha, "sha": sha, "candidate_sha": None, "started_at": timestamp(),
            "finished_at": None, "duration_seconds": None, "status": "active",
            "model": model(self.cfg.get("providers", {}).get(provider, {}).get("model"),
                           applicable=provider is not None),
            "usage": usage(), "provenance": provenance(settings, job),
            "configured_timeout_seconds": settings["service"].get("step_timeout_seconds")
                                          if provider or role == "checks" else None,
            "result": None, "error": None, "explanation": explanation(evidence=refs),
            "commands": list(commands), "evidence_file": file.name,
            "purpose": ("candidate" if sha == job.get("candidate") else "base")
                       if role == "checks" else None,
            "previous_attempt": previous.get("id"),
            "retry_of": previous.get("id") if previous.get("status") in
                        {"failed", "interrupted", "cancelled", "changes_requested", "pending"}
                        else None,
            "trigger": job.get("reason") or job.get("feedback"),
            "pr": job.get("pr_number"),
            "ci_origin": {"pr": job.get("pr_number"), "sha": repair["sha"], "key": repair["key"], "id": repair.get("id")}
                         if repair.get("key") and repair.get("sha") else None,
            "conflict_origin": job.get("pr_conflict"),
        }
        start = time.monotonic()
        record_stage(self.db, job, record, boundary="started")
        prior_stage = job.get("_stage")
        job["_stage"] = ident
        try:
            yield record
            if record["status"] == "active":
                record["status"] = "completed"
        except Exception as exc:
            reason = clean(str(exc))
            record.update(status="cancelled" if isinstance(exc, TaskStopped) else "failed",
                          error=reason, error_type=type(exc).__name__,
                          explanation=explanation(
                              reason, decisions=["Cerrar el intento"], reason=reason,
                              criteria=record["explanation"]["criteria"],
                              evidence=record["explanation"]["evidence"],
                          ))
            raise
        finally:
            if prior_stage is None:
                job.pop("_stage", None)
            else:
                job["_stage"] = prior_stage
            # A process exit or expired lease leaves the durable start for the next worker.
            if record["status"] != "active":
                record.update(finished_at=timestamp(),
                              duration_seconds=round(time.monotonic() - start, 3))
                try:
                    record_stage(self.db, job, record, boundary="finished")
                except TaskStopped:
                    pass
                else:
                    write_artifact(file, record)

    def test(self, sha):
        repo = self.job["repo"]
        current = next((item for item in self.cfg.get("repositories", [])
                        if item["slug"] == self.job["slug"]), {})
        # Reuse private argv values from the registry only when the saved command still matches.
        repo = repo | {key: current[key] for key in ("setup", "checks")
                       if key in current and filtered(current[key]) == repo[key]}
        folder = self.evidence / uuid.uuid4().hex
        artifact = str(folder.relative_to(self.evidence) / "checks.json")
        with self.stage("checks", sha, commands=repo["setup"] + repo["checks"],
                        evidence_files=[artifact]) as stage:
            workspace = self.checkout(sha)
            # Compose services can have a different numeric UID.
            workspace.chmod(0o755)
            report = checks.verify(
                repo, self.config, workspace, sha, folder, self.owner,
                self.cfg["service"]["step_timeout_seconds"], guard=self.guard,
            )
            stage["result"] = report
            if not report["passed"]:
                stage.update(status="failed", error=report.get("failure_kind") or "checks_failed")
            summary = "Checks correctos" if report["passed"] else "Fallaron los checks"
            stage["explanation"] = explanation(
                summary, decisions=[summary], criteria=["Códigos de salida de los comandos configurados"],
                evidence=stage["explanation"]["evidence"], reason=stage["error"],
            )
            report["stage_attempt"] = stage["id"]
        return filtered(report)

    def role(self, role, sha, folder, **inputs):
        job = self.job
        require_lease(self.db, job)
        provider = job["repo"]["routing"][role]
        settings = self.cfg
        timeout = (job.get("stage_timeouts") or {}).get(role)
        if timeout is not None:
            settings = self.cfg | {"service": self.cfg["service"] | {"step_timeout_seconds": timeout}}
        with self.stage(role, sha, provider=provider, settings=settings) as stage:
            stage["explanation"]["criteria"] = (inputs.get("plan") or {}).get("criteria", [])
            activity_file = self.evidence / f"{role}-{stage['id']}.activity.jsonl"
            last_activity_log = 0
            started = time.monotonic()

            def activity(event):
                nonlocal last_activity_log
                if event.get("event") == "cli_version":
                    stage["cli_version"] = event["version"]
                for key in ("model", "execution_id", "execution_id_source"):
                    if key in event:
                        stage[key] = event[key]
                if "usage_evidence" in event:
                    stage["usage"] = event["usage_evidence"]
                record = {"at": timestamp(), "role": role, "sha": sha,
                          "attempt": stage["id"], **event}
                with activity_file.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(filtered(record)) + "\n")
                new_file = activity_file.name not in stage["explanation"]["evidence"]
                if new_file:
                    stage["explanation"]["evidence"].append(activity_file.name)
                if new_file or event.get("event") in {"cli_version", "provider_metadata"}:
                    record_stage(self.db, job, stage)
                now = time.monotonic()
                if (event.get("event") == "cli_version" or now - last_activity_log >= 15
                        or event.get("type") == "result"):
                    label = event.get("type") or event.get("event")
                    print(clean(f"{job['slug']}#{job['number']} {role} {provider} {label} ({now - started:.0f}s)"),
                          flush=True)
                    last_activity_log = now

            try:
                if role in (job.get("routing_override") or {}) and not settings["providers"].get(provider, {}).get("enabled"):
                    raise RuntimeError("El proveedor asignado a este trabajo está deshabilitado")
                if job.get("operator_feedback"):
                    inputs["operator_feedback"] = job["operator_feedback"]
                result = providers.invoke(
                    role, job, settings, folder,
                    dict(title=job["title"], body=job["body"], sha=sha, **inputs),
                    self.owner, guard=self.guard, on_activity=activity,
                )
            except Exception as exc:
                if role == "build" and job["state"] == "building" and not isinstance(exc, TaskStopped):
                    checkpoint = dict(job.get("build_workspace") or {})
                    checkpoint.update(error=clean(str(exc)), failed_attempt=stage["id"])
                    self.update("building", build_workspace=checkpoint)
                raise
            for key in ("execution_id", "execution_id_source"):
                if key in result:
                    stage[key] = result[key]
            if isinstance(result.get("model"), dict):
                stage["model"] = result["model"]
            stage["cli_version"] = result.get("version", stage["cli_version"])
            stage["usage"] = result.get("usage_evidence", stage["usage"])
            stage["result"] = result["result"]
            if result["result"].get("question"):
                stage["status"] = "needs_input"
            elif result["result"].get("verdict") == "changes_requested":
                stage["status"] = "changes_requested"
            stage["explanation"] = agent_explanation(
                role, result["result"], stage["explanation"]["evidence"], inputs.get("plan"),
            )
            result.update(sha=sha, attempt=job.get("attempt"), stage_attempt=stage["id"],
                          evidence_file=stage["evidence_file"])
        return filtered(result)

    def admission(self):
        return ci.scope(self.db, self.job, api)

    def recover(self):
        ids = command(
            ["docker", "ps", "-aq", "--filter", "label=masf.owner=" + self.owner],
            env=docker_env(),
        ).split()
        if ids:
            command(["docker", "rm", "-f", *ids], env=docker_env())
        for file in self.evidence.glob("*/cleanup.json"):
            stop, workspace = json.loads(file.read_text())
            command(stop, env=docker_env() | {"MASF_WORKSPACE": workspace})
            checks.remove_profiles(stop, Path(workspace))
            file.unlink()

    def merge_tree(self, base, candidate):
        try:
            return self.git("merge-tree", "--write-tree", "-z", base, candidate).rstrip("\0"), []
        except ProcessError as exc:
            if exc.returncode != 1:
                raise
            tree, *entries = exc.stdout.split("\0")
            if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", tree):
                raise
            paths = set()
            index = 0
            while entries[index]:
                paths.add(entries[index].split("\t", 1)[1])
                index += 1
            index += 1
            while index < len(entries) and entries[index]:
                count = int(entries[index])
                if entries[index + count + 1].startswith("CONFLICT"):
                    paths.update(entries[index + 1:index + count + 1])
                index += count + 3
            if not paths:
                raise
            return tree, sorted(paths)

    def check_resolution(self, candidate, base, head, paths):
        for ancestor in (base, head):
            try:
                self.git("merge-base", "--is-ancestor", ancestor, candidate)
            except ProcessError as exc:
                if exc.returncode != 1:
                    raise
                raise RecoverableFailure(
                    "La resolución debe conservar el head de entrada y la base como ancestros"
                ) from None
        changed = self.git("diff", "--no-renames", "--name-only", "-z", base, candidate)
        tracked = {entry.split("\t", 1)[1] for entry in
                   self.git("ls-tree", "-r", "-z", candidate).split("\0")
                   if entry and entry.split(" ", 2)[1] == "blob"}
        for path in sorted((set(changed.split("\0")) | set(paths)) & tracked):
            content = self.git("show", f"{candidate}:{path}")
            if re.search(r"^(?:<{7,}|={7,}|>{7,}|\|{7,})(?:[ \t].*)?$", content, re.MULTILINE) or (
                path in paths and re.search(r"^<+ .+\n[\s\S]*?^=+\n[\s\S]*?^>+ .+$", content, re.MULTILINE)
            ):
                raise RecoverableFailure("La resolución conservó marcadores de conflicto")

    def start_base_conflict(self, base, candidate, tree, paths, *, pull=None):
        j = self.job
        conflict = {"base": base, "candidate": candidate, "paths": paths, "fingerprint": j["fingerprint"]}
        action = "Resolver el árbol combinado y preservar los criterios de la issue y el comportamiento de la base"
        updates = {"base_conflict": conflict}
        if pull:
            pull = pull | {"paths": paths, "status": "confirmed", "decision": action}
            updates.update(pr_conflict=pull, integration_approval=None,
                           candidate=candidate, base_checked=None, checks={}, review={}, repair_intent=None,
                           build_workspace=None, build_recovery=None)
        self.git("update-ref", "refs/masf/conflict-tree", tree)
        self.update("building" if pull else "publishing", **updates)
        try:
            history = reserve(j, self.cfg, "resolve_base", reason=action, **conflict)
        except BudgetExhausted as exc:
            raise BudgetExhausted(
                f"{exc}. Conflicto en {', '.join(paths)}. Usa resume --resolved-commit SHA"
            ) from None
        updates = dict(base=base, recoveries=history, repair_intent=None, build_workspace=None,
                       build_recovery=conflict | {"type": "base_conflict", "tree": tree},
                       feedback={"type": "base_conflict", "paths": paths, "instruction": action})
        if pull:
            updates["pr_conflict"] = pull | {"status": "resolving", "attempt": len(history)}
        else:
            base_report = self.test(base)
            if not base_report["passed"]:
                if base_report.get("retryable"):
                    raise RecoverableFailure("Infraestructura temporal en la nueva base")
                raise RuntimeError("Fallan los checks de la nueva base")
            updates["base_checked"] = base
        if pull and len(history) > 1:
            self.update("retry_wait", recovery_wait=wait(self.cfg, history, "building"), **updates)
        else:
            self.update("building", **updates)

    def remote_base(self):
        output = self.git("ls-remote", "--refs", self.url, "refs/heads/" + self.job["branch"])
        return output.split()[0] if output else None

    def pending_merge(self, reason, *, pr=None):
        j = self.job
        observation = {"pr": j["pr_number"], "fingerprint": j["fingerprint"], "status": "pending",
                       "base": (pr or {}).get("base", {}).get("sha"),
                       "head": (pr or {}).get("head", {}).get("sha")}
        self.update(j["state"], merge_observation=observation, reason=reason,
                    ci_retry_at=time.time() + max(retry_delay(), self.cfg["service"].get("ci_retry_seconds", 60)))

    def defer_pr_observation(self, exc):
        if re.search(ci.SIGNALS["access"], str(exc), re.I) and not retry_delay():
            raise RuntimeError("Acceso insuficiente. Revisa los permisos de GitHub y usa resume") from exc
        self.pending_merge("Observación del PR pendiente: " + ci.excerpt(str(exc)))
        return True

    def sync_pr_conflict(self):
        """Validate the PR and its Git refs, then refresh recovery if its input changed."""
        j = self.job
        conflict = j["pr_conflict"]
        try:
            pr = ci.pull(j, api)
            head = ci.expected_head(j)
            if not ci.validate(self.db, j, pr, {head}) or not self.admission():
                return False
            remote_head = self.remote_head()
            if remote_head != head:
                ci.head_conflict(self.db, j, pr["head"]["sha"], remote_head)
                return False
            base = self.remote_base()
        except (OSError, ProcessError, KeyError, TypeError, ValueError) as exc:
            self.defer_pr_observation(exc)
            return False
        if not base or base != pr["base"].get("sha"):
            self.pending_merge("GitHub aún no refleja la base vigente del PR", pr=pr)
            return False
        current = (base, head, j["fingerprint"])
        recorded = (conflict["base"], conflict["head"], conflict["fingerprint"])
        if current == recorded and conflict["status"] in {"resolving", "resolved"}:
            return True
        status = ci.merge_status(pr)
        if status == "pending":
            self.pending_merge("Mergeabilidad del PR pendiente en GitHub", pr=pr)
            return False
        for sha in (base, head):
            self.git("fetch", "--no-tags", self.url, sha)
        tree, paths = self.merge_tree(base, head)
        observation = {"pr": pr["number"], "base": base, "head": head,
                       "fingerprint": j["fingerprint"], "status": status}
        if not paths:
            outcome = "disagreement" if status == "conflicting" else "compatible"
            reason = ("Git no confirma el conflicto de GitHub para estas referencias"
                      if outcome == "disagreement" else "La base vigente es compatible con el head")
            self.update("pr_open", merge_observation=observation | {
                            "status": "pending" if outcome == "disagreement" else outcome},
                        pr_conflict=None, conflict_result=observation | {"status": outcome, "decision": reason},
                        candidate=head, build_workspace=None,
                        build_recovery=None, base_conflict=None, repair_intent=None,
                        **{key: j.get(key, {}) if (j.get(key) or {}).get("sha") == head else {}
                           for key in ("checks", "review")},
                        integration_approval=None, reason=reason, ci_retry_at=0)
            return False
        if status != "conflicting":
            self.pending_merge("Git y GitHub discrepan sobre la mergeabilidad vigente", pr=pr)
            return False
        self.update(j["state"], merge_observation=observation, ci_retry_at=0)
        self.start_base_conflict(base, head, tree, paths, pull=observation)
        return j["state"] != "retry_wait"

    def process(self):
        j = self.job
        repo = j["repo"]
        interrupt_stages(self.db, j)
        self.update(j["state"])
        self.recover()
        if j.get("repair_intent") and self.publication(self.publish_repair, recover=True):
            return
        if j.get("pr_number") and not j.get("pr_conflict"):
            if not self.pr_head():
                return
            if j.get("ci_repair") and j["state"] in {"building", "checking"}:
                try:
                    if not ci.observe_and_route(self.db, j, self.cfg, api, revalidate=True):
                        return
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    ci.poll_failed(self.db, j, self.cfg, ci.excerpt(str(exc)))
                    return
                if j.get("ci_retry_at", 0) > time.time():
                    return
        elif not j.get("pr_number") and j.get("intent") and self.publication(self.reconcile_publication):
            return
        if not self.store.exists():
            command(["git", "init", "--bare", str(self.store)])
        if j.get("pr_conflict"):
            if not self.sync_pr_conflict():
                return
        elif not self.admission():
            return
        self.git("fetch", "--no-tags", self.url, j["base"])
        if j.get("pr_number"):
            self.git("fetch", "--no-tags", self.url, ci.expected_head(j))
        if j.get("base_checked") != j["base"]:
            report = self.test(j["base"])
            if not report["passed"]:
                if report.get("retryable"):
                    raise RecoverableFailure(
                        "Infraestructura temporal al comprobar la base"
                    )
                commands = report.get("commands")
                detail = f": {commands[-1]}" if commands else ""
                raise RuntimeError("Fallan los checks de la base" + detail)
            self.update(j["state"] if j.get("plan") else "planning", base_checked=j["base"])
        if not j.get("plan"):
            planning = self.role("plan", j["base"], self.checkout(j["base"]))
            result = planning["result"]
            if "question" in result:
                self.update("needs_input", reason=result["question"], retry="planning",
                            plan_attempt=planning.get("stage_attempt"))
                return
            self.update("building", plan=result, reason="", plan_attempt=planning.get("stage_attempt"))
        while self.admission():
            conflict = j.get("pr_conflict")
            candidate = j.get("candidate")
            if not candidate or j["state"] == "building":
                if conflict:
                    base = j["base"]
                    if not self.sync_pr_conflict() or j["base"] != base:
                        return
                elif j.get("pr_number") and not self.pr_head():
                    return
                recovery = j.get("build_recovery") or {}
                checkpoint = j.get("build_workspace") or {}
                operator_resume = checkpoint.get("operator_resume")
                if not operator_resume and not checkpoint.get("result") and (
                    recovery.get("started") or (
                        not recovery and checkpoint
                        and limit(j, self.cfg)
                    )
                ):
                    history = reserve(j, self.cfg, "resume_build", candidate=candidate,
                                      reason=checkpoint.get("error") or "Construcción interrumpida")
                    recovery = {**(recovery or {"type": "retry"}), "started": False}
                    if conflict:
                        reason = "Resolución interrumpida. Reintento programado"
                        self.update("retry_wait", recoveries=history, build_recovery=recovery,
                                    recovery_wait=wait(self.cfg, history, "building"),
                                    reason=reason, pr_conflict=conflict | {
                                        "attempt": len(history), "decision": reason})
                        return
                    self.update("building", recoveries=history, build_recovery=recovery)
                normal_rounds = j["rounds"] - j.get("recovery_builds", 0)
                counts = ci.limits(j, self.cfg)
                if (
                    not recovery
                    and not operator_resume
                    and not j.get("pr_number")
                    and normal_rounds >= counts["build_limit"]
                ):
                    raise RuntimeError("Rondas de construcción agotadas")
                start = candidate or j["base"]
                reused = (
                    checkpoint.get("input_sha") == start
                    and checkpoint.get("fingerprint") == j["fingerprint"]
                )
                same_round = bool(
                    (j.get("pr_number") or operator_resume) and reused and checkpoint.get("round")
                )
                if not recovery and not same_round and normal_rounds >= counts["build_limit"]:
                    if j.get("pr_number"):
                        self.update("needs_input", retry="building", ci_intervention=True,
                                    reason=f"Rondas agotadas ({j['rounds']}/{counts['build_limit']}). Usa resume --add-rounds N")
                        return
                    raise RuntimeError("Rondas de construcción agotadas")
                workspace = (
                    saved_workspace(self.root, checkpoint["path"])
                    if reused
                    else self.checkout(recovery.get("tree", start))
                )
                checkpoint = {
                    "path": str(workspace),
                    "input_sha": start,
                    "fingerprint": j["fingerprint"],
                    "error": checkpoint.get("error") if reused else None,
                    "round": checkpoint["round"] if operator_resume and same_round
                             else j["rounds"] if same_round else j["rounds"] + 1,
                    **({"result": checkpoint["result"]} if same_round and "result" in checkpoint else {}),
                }
                if not same_round:
                    fields = {}
                    if j.get("pr_number") and not conflict:
                        fields["ci_rounds"] = j.get("ci_rounds", 0) + 1
                    self.update(
                        "building", rounds=checkpoint["round"], build_workspace=checkpoint,
                        recovery_builds=j.get("recovery_builds", 0) + bool(recovery),
                        build_recovery={**recovery, "started": True} if recovery else None,
                        **fields,
                    )
                build = checkpoint.get("result")
                if build is None:
                    if same_round:
                        self.update("building", build_workspace=checkpoint,
                                    build_recovery={**recovery, "started": True} if recovery else None)
                    build = self.role(
                        "build", start, workspace, plan=j["plan"], feedback=j.get("feedback"),
                        recovery={"workspace_reused": reused,
                                  "previous_error": checkpoint.get("error"), **recovery},
                    )
                    if j.get("pr_number"):
                        checkpoint["result"] = build
                        self.update("building", build_workspace=checkpoint)
                if j.get("pr_number") and (not self.admission() or not self.pr_head()):
                    return
                self.git("read-tree", start)
                if any(
                    name.lower() == ".git"
                    for _, dirs, files in os.walk(workspace)
                    for name in dirs + files
                ):
                    raise RuntimeError("Metadatos Git dentro de la copia del agente")
                self.git("add", "-A", "--", ".", workspace=workspace)
                tree = self.git("write-tree")
                parent = start if j.get("pr_number") and recovery.get("type") != "base_conflict" else j["base"]
                if (recovery.get("type") != "base_conflict" or not j.get("pr_number")) and (
                    tree == self.git("rev-parse", parent + "^{tree}")
                ):
                    if j.get("pr_number"):
                        self.update("building", build=build, build_workspace=None,
                                    feedback={"previous": j.get("feedback"),
                                              "reason": "El agente no ha producido cambios"})
                        continue
                    raise RuntimeError("El agente no ha producido cambios")
                parents = ["-p", parent]
                if recovery.get("type") == "base_conflict":
                    parents += ["-p", recovery["candidate"]]
                candidate = self.git("commit-tree", tree, *parents, "-m", j["title"])
                if recovery.get("type") == "base_conflict":
                    try:
                        self.check_resolution(candidate, j["base"], recovery["candidate"], recovery["paths"])
                    except RecoverableFailure as exc:
                        if conflict:
                            self.git("update-ref", f"refs/masf/invalid/{candidate}", candidate)
                            self.update("building", build=build, build_workspace=None,
                                        conflict_result=conflict | {"status": "invalid",
                                                                    "candidate": candidate, "decision": str(exc)})
                        raise
                self.git("update-ref", "refs/heads/candidate", candidate)
                resolved = {}
                if conflict:
                    conflict = conflict | {"status": "resolved", "candidate": candidate}
                    resolved["pr_conflict"] = conflict
                self.update(
                    "checking",
                    candidate=candidate,
                    build=build,
                    build_workspace=None,
                    build_recovery=None,
                    base_conflict=None,
                    **resolved,
                )
            paths = self.git(
                "diff", "--no-renames", "--name-only", "-z", j["base"], candidate
            ).split("\0")
            protected = [
                path for path in paths if path
                and j.get("path_approvals", {}).get(path, {}).get("fingerprint") != j["fingerprint"]
                and any(fnmatch.fnmatchcase(path, pattern) for pattern in repo.get("protected_paths", []))
            ]
            if protected:
                raise ProtectedPaths(protected)
            report = j.get("checks", {})
            if report.get("sha") != candidate or not report.get("passed"):
                report = self.test(candidate)
                if not report["passed"]:
                    if report.get("failure_kind") == "infrastructure":
                        if j.get("pr_number") and not conflict:
                            counts = ci.limits(j, self.cfg)
                            if counts["technical"] < counts["technical_limit"]:
                                self.update("checking", checks=report,
                                            ci_technical=counts["technical"] + 1,
                                            ci_retry_at=time.time() + self.cfg["service"].get("ci_retry_seconds", 60),
                                            reason="Fallo de infraestructura en checks. Reintento programado")
                            else:
                                self.update("blocked", checks=report, retry="checking",
                                            reason="Reintentos técnicos agotados. Corrige los checks y usa resume")
                            return
                        self.update("checking", checks=report)
                        if report.get("retryable"):
                            raise RecoverableFailure(
                                "Infraestructura temporal en checks"
                            )
                        raise RuntimeError(
                            "Fallo de infraestructura en checks, reanudar tras corregirlo"
                        )
                    self.update("building", feedback=report, checks=report)
                    if conflict:
                        self.retry_resolution(candidate, "Fallaron los checks de la resolución")
                    continue
                self.update("reviewing", checks=report)
            repair = j.get("ci_repair") or {}
            if candidate == ci.expected_head(j) and repair and all(
                item["kind"] == "infrastructure" for item in repair["diagnostics"]
            ):
                self.update("needs_input", retry="pr_open", ci_intervention=True,
                            checks=report, ci_retry_at=0,
                            reason="Checks locales correctos. Relanza el CI del PR y usa resume")
                return
            review = j.get("review", {})
            if review.get("sha") != candidate or review["result"]["verdict"] != "pass":
                diff = self.git("diff", "--no-ext-diff", j["base"], candidate)
                review = self.role(
                    "review",
                    candidate,
                    self.checkout(candidate),
                    plan=j["plan"],
                    diff=diff,
                    checks=report,
                )
                if review["result"]["verdict"] != "pass":
                    self.update("building", feedback=review["result"], review=review)
                    if conflict:
                        self.retry_resolution(candidate, "La revisión pidió cambios en la resolución")
                    continue
                self.update("publishing", review=review)
            if not self.admission():
                return
            if conflict:
                self.publication(self.publish_repair)
                return
            current = latest(j["slug"], j["branch"])
            if current != j["base"]:
                self.git("fetch", "--no-tags", self.url, current)
                tree, paths = self.merge_tree(current, candidate)
                if paths:
                    self.start_base_conflict(current, candidate, tree, paths)
                    continue
                prior = ["-p", candidate] if j.get("pr_number") else []
                rebased = self.git("commit-tree", tree, "-p", current, *prior, "-m", j["title"])
                history = reserve(
                    j, self.cfg, "refresh_base", base=current, candidate=candidate,
                    reason="Avanzó la rama base"
                )
                base_report = self.test(current)
                if not base_report["passed"]:
                    if base_report.get("retryable"):
                        raise RecoverableFailure(
                            "Infraestructura temporal en la nueva base"
                        )
                    raise RuntimeError("Fallan los checks de la nueva base")
                self.update(
                    "checking",
                    base=current,
                    base_checked=current,
                    candidate=rebased,
                    recoveries=history,
                    repair_intent=None,
                    base_conflict=None,
                )
                continue
            if j.get("pr_number"):
                self.publication(self.publish_repair)
            else:
                self.publication(self.publish)
            return

    def retry_resolution(self, candidate, reason):
        j = self.job
        conflict = j["pr_conflict"]
        self.update("building", build_workspace=None, build_recovery={
            "type": "base_conflict", "candidate": candidate, "base": j["base"],
            "paths": conflict["paths"], "tree": self.git("rev-parse", candidate + "^{tree}"),
        })
        raise RecoverableFailure(reason)

    def remote_head(self):
        args = ("ls-remote", self.url, "refs/heads/" + ci.branch(self.job))
        self.publication_command("git", *args)
        output = self.git(*args)
        return output.split()[0] if output else None

    def pr_head(self):
        j = self.job
        expected = ci.expected_head(j)
        pr = ci.pull(j, api)
        if not ci.validate(self.db, j, pr, {expected}):
            return False
        remote = self.remote_head()
        if remote != expected:
            ci.head_conflict(self.db, j, pr["head"]["sha"], remote)
            return False
        return True

    def publication(self, action, **options):
        job = self.job
        sha = job.get("candidate")
        with self.stage("publish", sha) as stage:
            self.publication_record = stage
            try:
                outcome = action(**options)
            finally:
                self.publication_record = None
            confirmed = job.get("published_head") == sha and bool(job.get("pr_number"))
            summary = "PR y SHA confirmados" if confirmed else "Publicación pendiente de confirmación"
            if job.get("reason") and not confirmed:
                summary = job["reason"]
            stage.update(
                status="completed" if confirmed else "pending",
                candidate_sha=sha, pr=job.get("pr_number"),
                result={"confirmed": confirmed, "pr": job.get("pr_number"),
                        "sha": job.get("published_head"), "url": job.get("pr"),
                        "intent": stage.get("publication_intent") or job.get("repair_intent") or job.get("intent"),
                        "intent_attempt": job.get("publication_intent_attempt")},
                explanation=explanation(
                    summary, decisions=[summary], criteria=["Checks y revisión aprobados para el SHA"],
                    evidence=[*stage["explanation"]["evidence"],
                              job.get("checks", {}).get("stage_attempt"),
                              job.get("review", {}).get("evidence_file"),
                              job.get("publication_intent_attempt")],
                    reason=None if confirmed else summary,
                ),
            )
            return outcome

    def publication_command(self, *argv):
        record = getattr(self, "publication_record", None)
        if record is not None:
            record["commands"].append(list(argv))
            record_stage(self.db, self.job, record)

    def publish_repair(self, *, recover=False):
        j = self.job
        sha = j["candidate"]
        expected = ci.expected_head(j)
        require_lease(self.db, j)
        try:
            pr = ci.pull(j, api)
        except (OSError, ProcessError, KeyError, TypeError) as exc:
            if not j.get("pr_conflict"):
                raise
            return self.defer_pr_observation(exc)
        if pr["state"] != "open":
            ci.validate(self.db, j, pr, set())
            return True
        intent = j.get("repair_intent")
        expected_intent = {"pr": j["pr_number"], "expected": expected, "target": sha}
        if j.get("pr_conflict"):
            expected_intent.update(base=j["base"], fingerprint=j["fingerprint"])
        if not (j.get("checks", {}).get("passed")
                and j["checks"]["sha"] == j.get("review", {}).get("sha") == sha
                and j["review"]["result"]["verdict"] == "pass"):
            raise RuntimeError("Falta la aprobación del SHA reparado")
        if not ci.validate(self.db, j, pr, {expected, sha} if intent else {expected}):
            return True
        try:
            remote = self.remote_head()
        except (OSError, ProcessError) as exc:
            if not j.get("pr_conflict"):
                raise
            return self.defer_pr_observation(exc)
        if intent:
            if intent != expected_intent:
                raise RuntimeError("Intención de actualización incompatible con el trabajo")
            if remote == sha:
                if pr["head"]["sha"] == sha:
                    conflict = j.get("pr_conflict")
                    self.update(
                        "pr_open", published_head=sha, repair_intent=None, ci_repair=None,
                        ci_intervention=False, ci_retry_at=0,
                        ci={"sha": sha, "state": "waiting", "checks": [], "pending_runs": []},
                        reason="",
                        **({"pr_conflict": None, "integration_approval": None,
                            "conflict_result": conflict | {"status": "published", "candidate": sha},
                            "merge_observation": {"pr": j["pr_number"], "base": j["base"],
                                                  "head": sha, "status": "pending",
                                                  "fingerprint": j["fingerprint"]}}
                           if conflict else {}),
                    )
                return True
            if remote != expected or pr["head"]["sha"] != expected:
                ci.head_conflict(self.db, j, pr["head"]["sha"], remote)
                return True
            if recover:
                # Recheck base, approvals and admission before replaying a push.
                return False
        elif remote != expected:
            ci.head_conflict(self.db, j, pr["head"]["sha"], remote)
            return True

        if sha == expected:
            raise RuntimeError("La reparación no produjo un SHA nuevo")
        if not j.get("pr_conflict") and (not self.admission() or not self.pr_head()):
            return True
        intent = expected_intent
        if getattr(self, "publication_record", None) is not None:
            self.publication_record["publication_intent"] = intent
        self.update("publishing", repair_intent=intent, publication_intent_attempt=j.get("_stage"))
        if j.get("pr_conflict"):
            self.check_resolution(sha, j["base"], expected, j["pr_conflict"]["paths"])
            if not self.sync_pr_conflict() or j.get("candidate") != sha:
                return True
        ref = "refs/heads/" + ci.branch(j)
        push = ("push", "--force-with-lease=" + ref + ":" + expected, self.url, sha + ":" + ref)
        self.publication_command("git", *push)
        try:
            self.git(*push, guard=lambda: require_lease(self.db, j))
        except (OSError, RuntimeError) as exc:
            if j.get("pr_conflict") and self.publish_repair(recover=True):
                return True
            if j.get("pr_conflict") and re.search(
                ci.SIGNALS["access"] + r"|protected branch|repository rule|write access", str(exc), re.I
            ):
                raise RuntimeError(
                    "Git rechazó el push. Revisa permisos y reglas de la rama del PR y usa resume"
                ) from exc
            raise
        return self.publish_repair(recover=True)

    def existing_pr(self):
        j = self.job
        params = urlencode(
            dict(
                head=j["slug"].split("/")[0] + f":masf/issue-{j['number']}",
                base=j["branch"],
                state="all",
            )
        )
        endpoint = f"repos/{j['slug']}/pulls?{params}"
        self.publication_command("gh", "api", endpoint)
        return api(endpoint)

    def record_pr(self, pr):
        j = self.job
        if (
            f"<!-- masf:{self.owner} -->" not in pr["body"]
            or pr["head"]["sha"] != j["candidate"]
            or pr["head"]["repo"]["full_name"] != j["slug"]
            or pr.get("base", {}).get("ref", j["branch"]) != j["branch"]
        ):
            raise RuntimeError("PR con procedencia o SHA inesperados")
        state = (
            "pr_open"
            if pr["state"] == "open"
            else ("merged" if pr["merged_at"] else "closed")
        )
        sha = j["candidate"]
        self.update(state, pr=pr["html_url"], pr_number=pr["number"], reason="",
                    published_head=sha,
                    ci={"sha": sha, "state": "waiting", "checks": [], "pending_runs": []})

    def reconcile_publication(self):
        j = self.job
        matches = self.existing_pr()
        if not matches:
            return False
        if not (
            j.get("checks", {}).get("passed")
            and j["checks"]["sha"]
            == j.get("review", {}).get("sha")
            == j.get("intent")
            == j.get("candidate")
            and j["review"]["result"]["verdict"] == "pass"
        ):
            raise RuntimeError("No se puede reconciliar un PR sin evidencia del SHA")
        self.record_pr(matches[0])
        return True

    def publish(self):
        j = self.job
        branch = f"masf/issue-{j['number']}"
        ref = "refs/heads/" + branch
        sha = j["candidate"]
        if not (
            j["checks"]["passed"]
            and j["checks"]["sha"] == j["review"]["sha"] == sha
            and j["review"]["result"]["verdict"] == "pass"
        ):
            raise ValueError("Falta la aprobación del SHA")
        self.publication_command("git", "ls-remote", self.url, ref)
        remote = self.git("ls-remote", self.url, ref)
        previous = remote.split()[0] if remote else ""
        if previous and previous not in (j.get("intent"), j.get("previous_intent")):
            raise RuntimeError("Rama remota con SHA inesperado o sin procedencia")
        self.update("publishing", intent=sha, previous_intent=previous,
                    publication_intent_attempt=j.get("_stage"))
        if previous != sha:
            require_lease(self.db, j)
            push = ("push", "--force-with-lease=" + ref + ":" + previous, self.url, sha + ":" + ref)
            self.publication_command("git", *push)
            self.git(*push)
        marker = f"<!-- masf:{self.owner} -->"
        matches = self.existing_pr()
        if matches:
            pr = matches[0]
        else:
            criteria = "\n".join("- " + c for c in j["plan"]["criteria"])
            summary = "\n".join(
                "- " + " ".join(c["argv"]) + " (0)" for c in j["checks"]["commands"]
            )
            body = f"{marker}\nCloses #{j['number']}\n\n{criteria}\n\nChecks sobre {sha}\n\n{summary}\n\nRevisión ({j['review']['provider']}): {j['review']['result']['summary']}"
            payload = dict(
                title=j["title"],
                body=clean(body),
                base=j["branch"],
                head=branch,
                draft=True,
            )
            endpoint = f"repos/{j['slug']}/pulls"
            self.publication_command("gh", "api", endpoint, "--input", "-")
            pr = api(endpoint, "--input", "-", stdin=json.dumps(filtered(payload)))
        self.record_pr(pr)
