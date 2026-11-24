import fnmatch
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from . import checks, ci, providers
from .queue import api, latest, require_lease, save
from .system import TaskStopped, clean, command, docker_env


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
        require_lease(self.db, self.job)
        if time.monotonic() >= self.next_admission:
            self.next_admission = time.monotonic() + 5
            if not self.admission() or (self.job.get("pr_number") and not self.pr_head()):
                raise TaskStopped("La tarea dejó de ser elegible")

    def git(self, *args, workspace=None, stdin=None, guard=None):
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
        options = {"guard": guard} if guard else {}
        return command(prefix + list(args), cwd=workspace or self.root, stdin=stdin, **options)

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
        require_lease(self.db, job)
        started = time.monotonic()
        file = self.evidence / f"{role}-{uuid.uuid4().hex}.json"
        try:
            result = providers.invoke(
                role,
                job,
                self.cfg,
                folder,
                dict(title=job["title"], body=job["body"], sha=sha, **inputs),
                self.owner,
                guard=self.guard,
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
                        "error": clean(str(exc)),
                        "seconds": round(time.monotonic() - started, 2),
                    },
                    indent=2,
                )
            )
            raise
        result.update(sha=sha, attempt=job.get("attempt"), evidence_file=file.name)
        file.write_text(clean(json.dumps(result, indent=2)))
        return result

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
            file.unlink()

    def process(self):
        j = self.job
        repo = j["repo"]
        self.update(j["state"])
        self.recover()
        if j.get("pr_number"):
            pr = ci.pull(j, api)
            if pr["state"] != "open":
                ci.validate(self.db, j, pr, set())
                return
            if not self.admission():
                return
            if j.get("repair_intent") and self.publish_repair(recover=True):
                return
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
        elif j.get("intent") and self.reconcile_publication():
            return
        if not self.admission():
            return
        if not self.store.exists():
            command(["git", "init", "--bare", str(self.store)])
        self.git("fetch", "--no-tags", self.url, j["base"])
        if j.get("pr_number"):
            self.git("fetch", "--no-tags", self.url, ci.expected_head(j))
        if j.get("base_checked") != j["base"]:
            report = self.test(j["base"])
            if not report["passed"]:
                raise RuntimeError(
                    "Fallan los checks de la base: " + str(report["commands"][-1])
                )
            self.update(j["state"] if j.get("pr_number") else "planning", base_checked=j["base"])
        if not j.get("plan"):
            result = self.role("plan", j["base"], self.checkout(j["base"]))["result"]
            if "question" in result:
                self.update("needs_input", reason=result["question"])
                return
            self.update("building", plan=result, reason="")
        while self.admission():
            candidate = j.get("candidate")
            if not candidate or j["state"] == "building":
                if j.get("pr_number") and not self.pr_head():
                    return
                start = candidate or j["base"]
                checkpoint = j.get("build_workspace") or {}
                reused = (
                    checkpoint.get("input_sha") == start
                    and checkpoint.get("fingerprint") == j["fingerprint"]
                )
                same_round = bool(
                    j.get("pr_number") and reused and checkpoint.get("round")
                )
                counts = ci.limits(j, self.cfg)
                if not same_round and j["rounds"] >= counts["build_limit"]:
                    if j.get("pr_number"):
                        self.update("needs_input", retry="building", ci_intervention=True,
                                    reason=f"Rondas agotadas ({j['rounds']}/{counts['build_limit']}). Usa resume --add-rounds N")
                        return
                    raise RuntimeError("Rondas de construcción agotadas")
                workspace = (
                    saved_workspace(self.root, checkpoint["path"])
                    if reused
                    else self.checkout(start)
                )
                checkpoint = {
                    "path": str(workspace),
                    "input_sha": start,
                    "fingerprint": j["fingerprint"],
                    "error": checkpoint.get("error") if reused else None,
                    "round": j["rounds"] if same_round else j["rounds"] + 1,
                    **({"result": checkpoint["result"]} if same_round and "result" in checkpoint else {}),
                }
                if not same_round:
                    fields = {}
                    if j.get("pr_number"):
                        fields["ci_rounds"] = j.get("ci_rounds", 0) + 1
                    self.update("building", rounds=checkpoint["round"], build_workspace=checkpoint, **fields)
                build = checkpoint.get("result")
                if build is None:
                    build = self.role(
                        "build",
                        start,
                        workspace,
                        plan=j["plan"],
                        feedback=j.get("feedback"),
                        recovery={
                            "workspace_reused": reused,
                            "previous_error": checkpoint.get("error"),
                        },
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
                parent = start if j.get("pr_number") else j["base"]
                if tree == self.git("rev-parse", parent + "^{tree}"):
                    if j.get("pr_number"):
                        self.update("building", build=build, build_workspace=None,
                                    feedback={"previous": j.get("feedback"),
                                              "reason": "El agente no ha producido cambios"})
                        continue
                    raise RuntimeError("El agente no ha producido cambios")
                candidate = self.git(
                    "commit-tree", tree, "-p", parent, "-m", j["title"]
                )
                self.git("update-ref", "refs/heads/candidate", candidate)
                self.update(
                    "checking", candidate=candidate, build=build, build_workspace=None
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
                        if j.get("pr_number"):
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
                        raise RuntimeError(
                            "Fallo de infraestructura en checks, reanudar tras corregirlo"
                        )
                    self.update("building", feedback=report, checks=report)
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
                    continue
                self.update("publishing", review=review)
            if not self.admission():
                return
            current = latest(j["slug"], j["branch"])
            if current != j["base"]:
                self.git("fetch", "--no-tags", self.url, current)
                tree = self.git("merge-tree", "--write-tree", current, candidate)
                prior = ["-p", candidate] if j.get("pr_number") else []
                rebased = self.git("commit-tree", tree, "-p", current, *prior, "-m", j["title"])
                if not self.test(current)["passed"]:
                    raise RuntimeError("Fallan los checks de la nueva base")
                self.update(
                    "checking", base=current, base_checked=current, candidate=rebased, repair_intent=None
                )
                continue
            if j.get("pr_number"):
                self.publish_repair()
            else:
                self.publish()
            return

    def remote_head(self):
        output = self.git("ls-remote", self.url, "refs/heads/" + ci.branch(self.job))
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

    def publish_repair(self, *, recover=False):
        j = self.job
        sha = j["candidate"]
        expected = ci.expected_head(j)
        require_lease(self.db, j)
        pr = ci.pull(j, api)
        if pr["state"] != "open":
            ci.validate(self.db, j, pr, set())
            return True
        if not self.admission():
            return True
        intent = j.get("repair_intent")
        if not (j.get("checks", {}).get("passed")
                and j["checks"]["sha"] == j.get("review", {}).get("sha") == sha
                and j["review"]["result"]["verdict"] == "pass"):
            raise RuntimeError("Falta la aprobación del SHA reparado")
        if not ci.validate(self.db, j, pr, {expected, sha} if intent else {expected}):
            return True
        remote = self.remote_head()
        if intent:
            if intent != {"pr": j["pr_number"], "expected": expected, "target": sha}:
                raise RuntimeError("Intención de actualización incompatible con el trabajo")
            if remote == sha:
                if pr["head"]["sha"] == sha:
                    self.update(
                        "pr_open", published_head=sha, repair_intent=None, ci_repair=None,
                        ci_intervention=False, ci_retry_at=0,
                        ci={"sha": sha, "state": "waiting", "checks": [], "pending_runs": []},
                        reason="",
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
        if not self.admission() or not self.pr_head():
            return True
        self.update("publishing", repair_intent={"pr": j["pr_number"], "expected": expected, "target": sha})
        ref = "refs/heads/" + ci.branch(j)
        self.git("push", "--force-with-lease=" + ref + ":" + expected,
                 self.url, sha + ":" + ref, guard=lambda: require_lease(self.db, j))
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
        remote = self.git("ls-remote", self.url, ref)
        previous = remote.split()[0] if remote else ""
        if previous and previous not in (j.get("intent"), j.get("previous_intent")):
            raise RuntimeError("Rama remota con SHA inesperado o sin procedencia")
        self.update("publishing", intent=sha, previous_intent=previous)
        if previous != sha:
            require_lease(self.db, j)
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
            pr = json.loads(
                command(
                    ["gh", "api", f"repos/{j['slug']}/pulls", "--input", "-"],
                    stdin=json.dumps(payload),
                )
            )
        self.record_pr(pr)
