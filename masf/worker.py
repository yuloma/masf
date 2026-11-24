import fnmatch
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from . import checks, providers
from .queue import api, eligible, fingerprint, latest, save
from .system import TaskStopped, clean, command, docker_env


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
                if j["rounds"] >= self.cfg["service"]["max_build_rounds"]:
                    raise RuntimeError("Rondas de construcción agotadas")
                start = candidate or j["base"]
                workspace = self.checkout(start)
                self.update("building", rounds=j["rounds"] + 1)
                build = self.role(
                    "build",
                    start,
                    workspace,
                    plan=j["plan"],
                    feedback=j.get("feedback"),
                )
                if any(
                    name.lower() == ".git"
                    for _, dirs, files in os.walk(workspace)
                    for name in dirs + files
                ):
                    raise RuntimeError("Metadatos Git dentro de la copia del agente")
                self.git("add", "-A", "--", ".", workspace=workspace)
                tree = self.git("write-tree")
                if tree == self.git("rev-parse", j["base"] + "^{tree}"):
                    raise RuntimeError("El agente no ha producido cambios")
                candidate = self.git(
                    "commit-tree", tree, "-p", j["base"], "-m", j["title"]
                )
                self.git("update-ref", "refs/heads/candidate", candidate)
                self.update("checking", candidate=candidate, build=build)
            paths = self.git(
                "diff", "--no-renames", "--name-only", "-z", j["base"], candidate
            ).split("\0")
            for path in paths:
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
                tree = self.git("merge-tree", "--write-tree", current, candidate)
                rebased = self.git("commit-tree", tree, "-p", current, "-m", j["title"])
                if not self.test(current)["passed"]:
                    raise RuntimeError("Fallan los checks de la nueva base")
                self.update(
                    "checking", base=current, base_checked=current, candidate=rebased
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
            pr = json.loads(
                command(
                    ["gh", "api", f"repos/{j['slug']}/pulls", "--input", "-"],
                    stdin=json.dumps(payload),
                )
            )
        self.record_pr(pr)
