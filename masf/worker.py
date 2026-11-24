from datetime import datetime, timezone
import fnmatch
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from . import checks, providers
from .queue import api, eligible, fingerprint, latest, save
from .recovery import RecoverableFailure, reserve
from .system import ProcessError, TaskStopped, clean, command, docker_env


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
        if time.monotonic() >= self.next_admission:
            self.next_admission = time.monotonic() + 5
            if not self.admission():
                raise TaskStopped("La tarea dejó de ser elegible")

    def git(self, *args, workspace=None, stdin=None):
        prefix = [
            "git",
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
        return command(prefix + list(args), cwd=workspace or self.root, stdin=stdin)

    def checkout(self, sha):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        self.git("read-tree", sha)
        self.git("checkout-index", "--all", workspace=folder)
        return folder

    def test(self, sha):
        folder = self.checkout(sha)
        # Compose services can have a different numeric UID.
        folder.chmod(0o755)
        return checks.verify(
            self.job["repo"],
            self.config,
            folder,
            sha,
            self.evidence / uuid.uuid4().hex,
            self.owner,
            self.cfg["service"]["step_timeout_seconds"],
            guard=self.guard,
        )

    def role(self, role, sha, folder, **inputs):
        job = self.job
        started = time.monotonic()
        file = self.evidence / f"{role}-{uuid.uuid4().hex}.json"
        activity_file = file.with_suffix(".activity.jsonl")
        activity_meta = {}
        last_activity_log = 0

        def activity(event):
            nonlocal last_activity_log
            activity_meta.update(event)
            record = {
                "at": datetime.now(timezone.utc).isoformat(),
                "role": role,
                "sha": sha,
                "attempt": job.get("attempt"),
                **event,
            }
            with activity_file.open("a") as stream:
                stream.write(clean(json.dumps(record)) + "\n")
            now = time.monotonic()
            if (
                event.get("event") == "cli_version"
                or now - last_activity_log >= 15
                or event.get("type") == "result"
            ):
                provider = job["repo"]["routing"][role]
                label = event.get("type") or event.get("event")
                tools = ",".join(event.get("tools", []))
                print(
                    f"{job['slug']}#{job['number']} {role} {provider} {label} {tools} ({now - started:.0f}s)",
                    flush=True,
                )
                last_activity_log = now

        try:
            result = providers.invoke(
                role,
                job,
                self.cfg,
                folder,
                dict(title=job["title"], body=job["body"], sha=sha, **inputs),
                self.owner,
                guard=self.guard,
                on_activity=activity,
            )
        except Exception as exc:
            if role == "build" and job["state"] == "building":
                checkpoint = dict(job.get("build_workspace") or {})
                checkpoint["error"] = clean(str(exc))
                self.update("building", build_workspace=checkpoint)
            file.write_text(
                json.dumps(
                    {
                        "sha": sha,
                        "role": role,
                        "attempt": job.get("attempt"),
                        "provider": job["repo"]["routing"][role],
                        "version": activity_meta.get("version"),
                        "last_activity": activity_meta,
                        "activity_file": str(activity_file),
                        "error": clean(str(exc)),
                        "seconds": round(time.monotonic() - started, 2),
                    },
                    indent=2,
                )
            )
            raise
        result.update(sha=sha, attempt=job.get("attempt"))
        file.write_text(clean(json.dumps(result, indent=2)))
        return result

    def admission(self):
        j = self.job
        issue = api(f"repos/{j['slug']}/issues/{j['number']}")
        if not eligible(issue, j["repo"]):
            self.update("cancelled", reason="Issue cerrada o etiqueta retirada")
        elif fingerprint(issue) != j["fingerprint"]:
            self.update("needs_input", reason="El contenido de la issue ha cambiado")
        else:
            return True
        return False

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
            file.unlink()

    def process(self):
        j = self.job
        repo = j["repo"]
        self.update(j["state"])
        self.recover()
        if j.get("intent") and self.reconcile_publication():
            return
        if not self.admission():
            return
        if not self.store.exists():
            command(["git", "init", "--bare", str(self.store)])
        self.git("fetch", "--no-tags", self.url, j["base"])
        if j.get("base_checked") != j["base"]:
            report = self.test(j["base"])
            if not report["passed"]:
                if report.get("retryable"):
                    raise RecoverableFailure(
                        "Infraestructura temporal al comprobar la base"
                    )
                raise RuntimeError(
                    "Fallan los checks de la base: " + str(report["commands"][-1])
                )
            self.update("planning", base_checked=j["base"])
        if not j.get("plan"):
            result = self.role("plan", j["base"], self.checkout(j["base"]))["result"]
            if "question" in result:
                self.update("needs_input", reason=result["question"])
                return
            self.update("building", plan=result)
        while self.admission():
            candidate = j.get("candidate")
            if not candidate or j["state"] == "building":
                recovery = j.get("build_recovery") or {}
                checkpoint = j.get("build_workspace") or {}
                if recovery.get("started") or (
                    not recovery
                    and checkpoint
                    and self.cfg["service"].get("max_recovery_attempts", 2)
                ):
                    history = reserve(j, self.cfg, "resume_build", candidate=candidate)
                    recovery = {**(recovery or {"type": "retry"}), "started": False}
                    self.update("building", recoveries=history, build_recovery=recovery)
                normal_rounds = j["rounds"] - j.get("recovery_builds", 0)
                if (
                    not recovery
                    and normal_rounds >= self.cfg["service"]["max_build_rounds"]
                ):
                    raise RuntimeError("Rondas de construcción agotadas")
                start = candidate or j["base"]
                reused = (
                    checkpoint.get("input_sha") == start
                    and checkpoint.get("fingerprint") == j["fingerprint"]
                )
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
                }
                self.update(
                    "building",
                    rounds=j["rounds"] + 1,
                    build_workspace=checkpoint,
                    recovery_builds=j.get("recovery_builds", 0) + bool(recovery),
                    build_recovery={**recovery, "started": True} if recovery else None,
                )
                build = self.role(
                    "build",
                    start,
                    workspace,
                    plan=j["plan"],
                    feedback=j.get("feedback"),
                    recovery={
                        "workspace_reused": reused,
                        "previous_error": checkpoint.get("error"),
                        **recovery,
                    },
                )
                if any(
                    name.lower() == ".git"
                    for _, dirs, files in os.walk(workspace)
                    for name in dirs + files
                ):
                    raise RuntimeError("Metadatos Git dentro de la copia del agente")
                self.git("add", "-A", "--", ".", workspace=workspace)
                tree = self.git("write-tree")
                if recovery.get("type") == "base_conflict":
                    tracked = set(
                        self.git("ls-tree", "-r", "--name-only", tree).splitlines()
                    )
                    for path in recovery["paths"]:
                        content = (
                            self.git("show", f"{tree}:{path}")
                            if path in tracked
                            else ""
                        )
                        if re.search(
                            r"^(?:<<<<<<< |=======\s*$|>>>>>>> )", content, re.MULTILINE
                        ):
                            raise RecoverableFailure(
                                "El agente conservó marcadores de conflicto"
                            )
                if tree == self.git("rev-parse", j["base"] + "^{tree}"):
                    raise RuntimeError("El agente no ha producido cambios")
                parents = ["-p", j["base"]]
                if recovery.get("type") == "base_conflict":
                    parents += ["-p", recovery["candidate"]]
                candidate = self.git("commit-tree", tree, *parents, "-m", j["title"])
                self.git("update-ref", "refs/heads/candidate", candidate)
                self.update(
                    "checking",
                    candidate=candidate,
                    build=build,
                    build_workspace=None,
                    build_recovery=None,
                    base_conflict=None,
                )
            paths = self.git(
                "diff", "--no-renames", "--name-only", "-z", j["base"], candidate
            ).split("\0")
            for path in paths:
                approval = j.get("path_approvals", {}).get(path, {})
                if approval.get("fingerprint") == j["fingerprint"]:
                    continue
                if any(
                    fnmatch.fnmatchcase(path, pattern)
                    for pattern in repo.get("protected_paths", [])
                ):
                    raise RuntimeError("Ruta protegida: " + path)
            report = j.get("checks", {})
            if report.get("sha") != candidate or not report.get("passed"):
                report = self.test(candidate)
                if not report["passed"]:
                    if report.get("failure_kind") == "infrastructure":
                        self.update("checking", checks=report)
                        if report.get("retryable"):
                            raise RecoverableFailure(
                                "Infraestructura temporal en checks"
                            )
                        raise RuntimeError(
                            "Fallo de infraestructura en checks, reanudar tras corregirlo"
                        )
                    self.update("building", feedback=report, checks=report)
                    continue
                self.update("reviewing", checks=report)
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
                    continue
                self.update("publishing", review=review)
            if not self.admission():
                return
            current = latest(j["slug"], j["branch"])
            if current != j["base"]:
                self.git("fetch", "--no-tags", self.url, current)
                try:
                    tree = self.git("merge-tree", "--write-tree", current, candidate)
                except ProcessError as exc:
                    if "CONFLICT" not in exc.stdout:
                        raise
                    paths = sorted(
                        set(
                            re.findall(
                                r"^\d+ [0-9a-f]+ [123]\t(.+)$", exc.stdout, re.MULTILINE
                            )
                        )
                    )
                    self.update(
                        "publishing",
                        base_conflict={
                            "base": current,
                            "candidate": candidate,
                            "paths": paths,
                        },
                    )
                    tree = exc.stdout.splitlines()[0]
                    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
                        raise RuntimeError(
                            "Git no devolvió un árbol de conflicto válido"
                        ) from None
                    try:
                        history = reserve(
                            j,
                            self.cfg,
                            "resolve_base",
                            base=current,
                            candidate=candidate,
                            paths=paths,
                        )
                    except RuntimeError as exhausted:
                        raise RuntimeError(
                            str(exhausted)
                            + ". Conflicto en "
                            + ", ".join(paths)
                            + ". Revisa la resolución y usa resume --resolved-commit SHA"
                        ) from None
                    base_report = self.test(current)
                    if not base_report["passed"]:
                        if base_report.get("retryable"):
                            raise RecoverableFailure(
                                "Infraestructura temporal en la nueva base"
                            )
                        raise RuntimeError("Fallan los checks de la nueva base")
                    self.update(
                        "building",
                        base=current,
                        base_checked=current,
                        recoveries=history,
                        build_workspace=None,
                        build_recovery={
                            "type": "base_conflict",
                            "tree": tree,
                            "base": current,
                            "candidate": candidate,
                            "paths": paths,
                        },
                        feedback={
                            "type": "base_conflict",
                            "paths": paths,
                            "instruction": "Resolve conflict markers preserving the issue criteria and current base behavior.",
                        },
                    )
                    continue
                rebased = self.git("commit-tree", tree, "-p", current, "-m", j["title"])
                history = reserve(
                    j, self.cfg, "refresh_base", base=current, candidate=candidate
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
                )
                continue
            self.publish()
            return

    def existing_pr(self):
        j = self.job
        params = urlencode(
            dict(
                head=j["slug"].split("/")[0] + f":masf/issue-{j['number']}",
                base=j["branch"],
                state="all",
            )
        )
        return api(f"repos/{j['slug']}/pulls?{params}")

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
        self.update(state, pr=pr["html_url"], pr_number=pr["number"], reason="")

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
        remote = self.git("ls-remote", self.url, ref)
        previous = remote.split()[0] if remote else ""
        if previous and previous not in (j.get("intent"), j.get("previous_intent")):
            raise RuntimeError("Rama remota con SHA inesperado o sin procedencia")
        self.update("publishing", intent=sha, previous_intent=previous)
        if previous != sha:
            self.git(
                "push",
                "--force-with-lease=" + ref + ":" + previous,
                self.url,
                sha + ":" + ref,
            )
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
            pr = api(
                f"repos/{j['slug']}/pulls", "--input", "-", stdin=json.dumps(payload)
            )
        self.record_pr(pr)
