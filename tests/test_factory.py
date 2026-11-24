import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from masf import config, providers, queue, worker
from masf.system import command

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples/factory.toml"

FAKE_GH = r"""
import json
import os
import sys
import time
from pathlib import Path

root = Path(__file__).parent
pid = str(os.getppid())
args = sys.argv[1:]
with (root / "requests" / pid).open("a") as log:
    print(json.dumps(args), file=log)

responses = json.loads((root / "responses.json").read_text())
if len(args) != 3 or args[:2] != ["api", "--include"] or args[2] not in responses:
    sys.exit(f"Unexpected gh invocation: {args!r}")

endpoint = args[2]
barrier = root / "barrier"
if barrier.is_dir() and endpoint == (barrier / "endpoint").read_text():
    (barrier / f"scan-{pid}").touch()
    deadline = time.monotonic() + 15
    while len(list(barrier.glob("scan-*"))) < 2:
        if time.monotonic() >= deadline:
            sys.exit("Timed out waiting for the second scan")
        time.sleep(0.02)

response = responses[endpoint]
if response is None:
    print('HTTP/2.0 403 Forbidden\n\n{"message": "Forbidden"}')
    sys.exit("gh: Forbidden (HTTP 403)")
print(json.dumps(response))
"""


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = config.load(CONFIG)
        self.repo = self.cfg["repositories"][0]
        self.repo["enabled"] = True
        self.issue = dict(
            number=1,
            state="open",
            title="Add a greeting",
            body="For Ana",
            labels=[{"name": self.repo["label"]}],
        )


class Discovery(TemporaryTest):
    def setUp(self):
        super().setUp()
        config_file = self.root / "config.toml"
        config_file.write_text(
            CONFIG.read_text().replace("enabled = false\nlabel", "enabled = true\nlabel")
        )
        profile = self.root / "profiles/postgres.yaml"
        profile.parent.mkdir()
        profile.write_bytes((CONFIG.parent / "profiles/postgres.yaml").read_bytes())
        self.scan_argv = [
            sys.executable, "-m", "masf.cli", "--config", str(config_file), "scan"
        ]
        self.state_home = self.root / "state"
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        self.requests = fake_bin / "requests"
        self.requests.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        fake_gh.chmod(0o755)
        self.responses_file = fake_bin / "responses.json"
        self.barrier = fake_bin / "barrier"
        self.scan_options = dict(
            cwd=ROOT,
            env={
                "PATH": str(fake_bin),
                "XDG_CONFIG_HOME": str(self.root),
                "XDG_STATE_HOME": str(self.state_home),
                "PYTHONUTF8": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        self.slugs = [repo["slug"] for repo in self.cfg["repositories"]]
        self.responses = {}
        self.issue_queries = []
        label = quote(self.repo["label"], safe="")
        for repo_id, slug in enumerate(self.slugs, start=1):
            endpoint = f"repos/{slug}"
            issues = f"{endpoint}/issues?state=open&labels={label}&per_page=100"
            self.issue_queries.extend([endpoint, f"{issues}&page=1", f"{issues}&page=2"])
            self.responses[endpoint] = {"id": repo_id, "default_branch": "main"}
            self.responses[f"{endpoint}/commits/main"] = {"sha": "a" * 40}
            self.responses[f"{issues}&page=1"] = [
                self.issue,
                self.issue | {"number": 3, "pull_request": {}},
                self.issue | {"number": 4, "state": "closed"},
                *[self.issue | {"number": 5, "labels": []}] * 97,
            ]
            self.responses[f"{issues}&page=2"] = [self.issue | {"number": 2}]

    def run_scan(self, *args):
        return subprocess.run(
            [*self.scan_argv, *args], timeout=30, **self.scan_options
        )

    def jobs(self):
        database = self.state_home / "masf/queue.sqlite3"
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
            return db.execute("SELECT repo_id, number, slug, state FROM jobs").fetchall()

    def state_snapshot(self):
        return {
            path.relative_to(self.state_home): (
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in (self.state_home, *self.state_home.rglob("*"))
        }

    def test_two_pages_two_repositories_concurrent_scans(self):
        self.responses_file.write_text(json.dumps(self.responses))
        self.barrier.mkdir()
        (self.barrier / "endpoint").write_text(f"repos/{self.slugs[0]}/commits/main")
        output = []
        with ExitStack() as stack:
            scans = []
            for _ in range(2):
                scan = stack.enter_context(
                    subprocess.Popen(self.scan_argv, **self.scan_options)
                )
                stack.callback(scan.kill)
                scans.append(scan)
            for scan in scans:
                stdout, stderr = scan.communicate(timeout=30)
                self.assertEqual(scan.returncode, 0, stderr)
                output.extend(stdout.splitlines())

        self.assertCountEqual(
            [path.name for path in self.barrier.glob("scan-*")],
            [f"scan-{scan.pid}" for scan in scans],
        )
        self.assertCountEqual(
            self.jobs(),
            [
                (repo_id, number, slug, "queued")
                for repo_id, slug in enumerate(self.slugs, start=1)
                for number in (1, 2)
            ],
        )
        self.assertCountEqual(
            output,
            [
                f"{slug}#{number} {status}"
                for slug in self.slugs
                for number in (1, 2)
                for status in ("queued", "ya registrado")
            ],
        )

    def test_access_failure_does_not_stop_next_repository(self):
        denied, accessible = self.slugs
        self.responses[f"repos/{denied}"] = None
        self.responses_file.write_text(json.dumps(self.responses))

        scan = self.run_scan()

        self.assertEqual(scan.returncode, 1, scan.stderr)
        self.assertRegex(scan.stderr, rf"(?m)^{re.escape(denied)}: .*HTTP 403")
        self.assertCountEqual(
            self.jobs(), [(2, 1, accessible, "queued"), (2, 2, accessible, "queued")]
        )

    def test_dry_run_preserves_absent_and_existing_state(self):
        self.responses_file.write_text(json.dumps(self.responses))
        for existing in (False, True):
            with self.subTest(existing_state=existing):
                if existing:
                    seed = self.run_scan()
                    self.assertEqual(seed.returncode, 0, seed.stderr)
                    self.assertEqual(len(self.jobs()), 4)
                    before = self.state_snapshot()
                for log in self.requests.iterdir():
                    log.unlink()

                scan = self.run_scan("--dry-run")

                self.assertEqual(scan.returncode, 0, scan.stderr)
                self.assertCountEqual(
                    scan.stdout.splitlines(),
                    [
                        f"{slug}#{number} elegible ({self.repo['label']})"
                        for slug in self.slugs
                        for number in (1, 2)
                    ],
                )
                self.assertCountEqual(
                    [
                        json.loads(line)
                        for log in self.requests.iterdir()
                        for line in log.read_text().splitlines()
                    ],
                    [["api", "--include", endpoint] for endpoint in self.issue_queries],
                )
                if existing:
                    self.assertEqual(self.state_snapshot(), before)
                else:
                    self.assertFalse(self.state_home.exists())

    def test_profile_traversal(self):
        file = self.root / "config.toml"
        file.write_text(
            CONFIG.read_text().replace("profiles/postgres.yaml", "../outside")
        )
        with self.assertRaisesRegex(ValueError, "compose.files"):
            config.load(file)


class Contracts(unittest.TestCase):
    def test_three_formats(self):
        result = dict(version=1, summary="ok", verdict="pass", findings=[])
        examples = {
            "cursor": {"result": json.dumps(result)},
            "claude": {"structured_output": result},
            "codex": {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(result)},
            },
        }
        for name, example in examples.items():
            with self.subTest(name=name):
                self.assertEqual(
                    providers.parse(
                        name,
                        "review",
                        json.dumps(example)
                        + ('\n{"type":"turn.completed"}' if name == "codex" else ""),
                    )[0],
                    result,
                )
                with self.assertRaises(ValueError):
                    providers.parse(name, "build", json.dumps(example))
                with self.assertRaises(ValueError):
                    providers.parse(name, "plan", "{")
                with self.assertRaises(RuntimeError):
                    providers.parse(
                        name,
                        "plan",
                        json.dumps({"type": "turn.failed", "is_error": True}),
                    )


class Delivery(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.source = self.root / "source"
        command(["git", "init", "-b", "main", str(self.source)])
        (self.source / "README").write_text("initial\n")
        command(["git", "add", "."], cwd=self.source)
        command(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-m",
                "base",
            ],
            cwd=self.source,
        )
        self.base = command(["git", "rev-parse", "HEAD"], cwd=self.source)
        db = queue.connect(self.root / "state")
        self.job = dict(
            slug=self.repo["slug"],
            repo_id=123,
            number=1,
            branch="main",
            repo=self.repo,
            title=self.issue["title"],
            body=self.issue["body"],
            state="queued",
            fingerprint=queue.fingerprint(self.issue),
            rounds=0,
            base=self.base,
        )
        db.execute(
            "INSERT INTO jobs VALUES(123,1,?,'queued',?)",
            (self.repo["slug"], json.dumps(self.job)),
        )
        db.commit()
        self.worker = worker.Worker(
            db, self.cfg, CONFIG, self.root / "state", self.root / "jobs", self.job
        )
        self.worker.url = str(self.source)
        self.prs, self.roles = [], []
        self.reject_check = self.reject_review = self.deny = self.crash = False
        self.changed = "greeting"
        for target, value in [
            (worker.providers, ("invoke", self.agent)),
            (worker, ("api", self.api)),
            (worker, ("latest", lambda *_: self.base)),
            (self.worker, ("recover", lambda: None)),
            (self.worker, ("test", self.check)),
            (worker, ("command", self.run_command)),
        ]:
            patcher = patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def agent(self, role, job, cfg, workspace, inputs, owner, *, guard=None, on_activity=None):
        self.roles.append(role)
        if role == "plan":
            result = {"criteria": ["AC1 greet"]}
        elif role == "build":
            file = workspace / self.changed
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(str(len(self.roles)))
            result = {"changes": ["AC1"]}
        else:
            self.assertIn("diff", inputs)
            self.assertEqual(inputs["checks"]["sha"], inputs["sha"])
            result = {
                "summary": "Reviewed",
                "verdict": "changes_requested" if self.reject_review else "pass",
            }
            self.reject_review = False
        return {"result": result, "provider": job["repo"]["routing"][role]}

    def check(self, sha):
        failed = sha != self.base and self.reject_check
        if failed:
            self.reject_check = False
        return dict(
            sha=sha,
            passed=not failed,
            commands=[{"argv": ["test"], "code": int(failed)}],
        )

    def api(self, endpoint):
        return self.prs if "pulls?" in endpoint else self.issue

    def run_command(self, args, **kwargs):
        if args[0] != "gh":
            return command(args, **kwargs)
        if self.deny:
            raise RuntimeError("403")
        payload = json.loads(kwargs["stdin"])
        pr = dict(
            head={
                "sha": self.job["candidate"],
                "repo": {"full_name": self.job["slug"]},
            },
            body=payload["body"],
            state="open",
            html_url="test",
            number=3,
        )
        self.prs.append(pr)
        if self.crash:
            raise RuntimeError("simulated lost response")
        return json.dumps(pr)

    def test_check_retry(self):
        self.reject_check = True
        self.worker.process()
        self.assertEqual(self.roles, ["plan", "build", "build", "review"])
        self.assertEqual(self.job["state"], "pr_open")

    def test_review_retry(self):
        self.reject_review = True
        self.worker.process()
        self.assertEqual(self.roles.count("build"), 2)
        self.assertEqual(self.job["review"]["sha"], self.prs[0]["head"]["sha"])

    def test_protected(self):
        self.changed = ".github/workflows/new.yml"
        with self.assertRaisesRegex(RuntimeError, "protegida"):
            self.worker.process()
        self.assertFalse(self.prs)

    def test_publication_recovery(self):
        self.deny = True
        with self.assertRaisesRegex(RuntimeError, "403"):
            self.worker.process()
        self.deny, self.crash = False, True
        with self.assertRaisesRegex(RuntimeError, "lost response"):
            self.worker.process()
        self.worker.process()
        self.assertEqual(self.roles, ["plan", "build", "review"])
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(self.job["state"], "pr_open")

    def test_issue_is_revalidated(self):
        self.issue["body"] += " edited"
        self.worker.process()
        self.assertEqual(self.job["state"], "needs_input")
        self.assertFalse(self.roles)
        self.issue["labels"] = []
        self.worker.process()
        self.assertEqual(self.job["state"], "cancelled")

    def test_failed_base_before_agents(self):
        with patch.object(
            self.worker, "test", return_value={"passed": False, "commands": ["failed"]}
        ):
            with self.assertRaisesRegex(RuntimeError, "base"):
                self.worker.process()
        self.assertFalse(self.roles)

    def test_third_party_branch(self):
        command(["git", "branch", "masf/issue-1"], cwd=self.source)
        with self.assertRaisesRegex(RuntimeError, "SHA inesperado"):
            self.worker.process()
        self.assertEqual(
            command(["git", "rev-parse", "masf/issue-1"], cwd=self.source), self.base
        )
        self.assertFalse(self.prs)

    def test_resume_preserves_verified_sha(self):
        from masf.cli import resume

        self.deny = True
        with self.assertRaisesRegex(RuntimeError, "403"):
            self.worker.process()
        queue.save(self.worker.db, self.job, "blocked", retry="publishing")
        with patch("masf.cli.api", self.api):
            resume(self.worker.db, self.cfg, f"{self.repo['slug']}#1")
        restored = json.loads(
            self.worker.db.execute("SELECT data FROM jobs").fetchone()[0]
        )
        self.assertEqual(restored["candidate"], self.job["candidate"])
        self.assertEqual(restored["state"], "publishing")

    def test_recover_created_pr_after_base_advanced(self):
        self.crash = True
        with self.assertRaisesRegex(RuntimeError, "lost response"):
            self.worker.process()
        self.crash = False
        original = self.job["candidate"]
        (self.source / "other").write_text("new base")
        command(["git", "add", "."], cwd=self.source)
        command(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-m",
                "advance",
            ],
            cwd=self.source,
        )
        self.base = command(["git", "rev-parse", "HEAD"], cwd=self.source)
        self.worker.process()
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(self.job["candidate"], original)
        self.assertEqual(self.roles, ["plan", "build", "review"])
        self.assertEqual(len(self.prs), 1)

    def test_recover_pushed_branch_after_base_advanced(self):
        self.deny = True
        with self.assertRaisesRegex(RuntimeError, "403"):
            self.worker.process()
        previous = self.job["candidate"]
        (self.source / "other").write_text("new base")
        command(["git", "add", "."], cwd=self.source)
        command(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-m",
                "advance",
            ],
            cwd=self.source,
        )
        self.base = command(["git", "rev-parse", "HEAD"], cwd=self.source)
        self.deny = False
        self.worker.process()
        self.assertEqual(self.job["state"], "pr_open")
        self.assertNotEqual(self.job["candidate"], previous)
        self.assertEqual(self.roles, ["plan", "build", "review", "review"])
        self.assertEqual(self.job["checks"]["sha"], self.job["candidate"])

    def test_infrastructure_failure_resumes_checks(self):
        original = self.worker.test

        def failing_check(sha):
            if sha == self.base:
                return original(sha)
            return {
                "sha": sha,
                "passed": False,
                "failure_kind": "infrastructure",
                "commands": [],
            }

        self.worker.test = failing_check
        with self.assertRaisesRegex(RuntimeError, "infraestructura"):
            self.worker.process()
        candidate = self.job["candidate"]
        self.assertEqual(self.job["state"], "checking")
        self.assertEqual(self.job["rounds"], 1)
        self.worker.test = original
        self.worker.process()
        self.assertEqual(self.job["candidate"], candidate)
        self.assertEqual(self.roles, ["plan", "build", "review"])

    def test_guard_preserves_cancelled_state(self):
        from masf.system import TaskStopped

        self.issue["labels"] = []
        with self.assertRaises(TaskStopped):
            self.worker.guard()
        self.assertEqual(self.job["state"], "cancelled")

    def test_resume_approves_one_path_for_one_job(self):
        from masf.cli import resume

        self.changed = ".github/workflows/factory.yml"
        with self.assertRaisesRegex(RuntimeError, "protegida"):
            self.worker.process()
        candidate = self.job["candidate"]
        queue.save(
            self.worker.db,
            self.job,
            "blocked",
            retry="checking",
            reason="Ruta protegida",
        )
        with patch("masf.cli.api", self.api):
            resume(
                self.worker.db,
                self.cfg,
                f"{self.repo['slug']}#1",
                [".github/workflows/factory.yml"],
            )
        restored = json.loads(
            self.worker.db.execute("SELECT data FROM jobs").fetchone()[0]
        )
        self.assertEqual(restored["candidate"], candidate)
        self.assertEqual(restored["rounds"], 1)
        self.assertEqual(restored["repo"]["protected_paths"], [".github/workflows/**"])
        self.worker.job = restored
        self.worker.process()
        self.assertEqual(restored["state"], "pr_open")
        self.assertEqual(self.roles, ["plan", "build", "review"])

    def test_path_approval_preserves_other_protections(self):
        self.changed = ".github/workflows/other.yml"
        self.job["path_approvals"] = {
            ".github/workflows/factory.yml": {"fingerprint": self.job["fingerprint"]}
        }
        with self.assertRaisesRegex(RuntimeError, "protegida"):
            self.worker.process()

    def test_stale_scope_invalidates_path_approval(self):
        self.changed = ".github/workflows/factory.yml"
        self.job["path_approvals"] = {self.changed: {"fingerprint": "old scope"}}
        with self.assertRaisesRegex(RuntimeError, "protegida"):
            self.worker.process()

    def test_resume_rejects_broad_or_noncanonical_paths(self):
        from masf.cli import resume

        self.changed = ".github/workflows/factory.yml"
        with self.assertRaisesRegex(RuntimeError, "protegida"):
            self.worker.process()
        queue.save(self.worker.db, self.job, "blocked", retry="checking")
        for path in (
            ".github/**",
            "/.github/workflows/factory.yml",
            "../secret",
            ".github/../factory.yml",
            ".github//workflows/factory.yml",
        ):
            with self.subTest(path=path), patch("masf.cli.api", self.api):
                with self.assertRaisesRegex(ValueError, "ruta protegida exacta"):
                    resume(self.worker.db, self.cfg, f"{self.repo['slug']}#1", [path])
        stored = json.loads(
            self.worker.db.execute("SELECT data FROM jobs").fetchone()[0]
        )
        self.assertNotIn("path_approvals", stored)

    def test_interrupted_build_reuses_saved_workspace(self):
        first = True
        original = self.agent
        seen = []

        def interrupted(role, job, cfg, workspace, inputs, owner, **kwargs):
            nonlocal first
            if role == "build":
                seen.append(workspace)
                if first:
                    first = False
                    (workspace / "partial.txt").write_text("preserved")
                    raise RuntimeError("docker: tiempo agotado")
                self.assertEqual((workspace / "partial.txt").read_text(), "preserved")
                self.assertTrue(inputs["recovery"]["workspace_reused"])
            return original(role, job, cfg, workspace, inputs, owner, **kwargs)

        with patch.object(worker.providers, "invoke", interrupted):
            with self.assertRaisesRegex(RuntimeError, "tiempo agotado"):
                self.worker.process()
            stored = json.loads(
                self.worker.db.execute("SELECT data FROM jobs").fetchone()[0]
            )
            self.assertEqual(stored["build_workspace"]["path"], str(seen[0]))
            self.worker.process()
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(self.job["rounds"], 2)
        self.assertIsNone(self.job["build_workspace"])

    def test_resume_recovers_legacy_workspace(self):
        from masf.cli import resume

        folder = (
            self.worker.checkout(self.base)
            if self.worker.store.exists()
            else self.worker.root / "legacy"
        )
        folder.mkdir(exist_ok=True)
        (folder / "partial.txt").write_text("preserved")
        queue.save(
            self.worker.db, self.job, "blocked", retry="building", reason="timeout"
        )
        with (
            patch("masf.cli.api", self.api),
            patch.dict("os.environ", {"MASF_JOBS_DIR": str(self.root / "jobs")}),
        ):
            resume(
                self.worker.db,
                self.cfg,
                f"{self.repo['slug']}#1",
                reuse_workspace=folder,
            )
        j = json.loads(self.worker.db.execute("SELECT data FROM jobs").fetchone()[0])
        self.assertEqual(j["build_workspace"]["path"], str(folder))
        self.assertEqual(j["build_workspace"]["input_sha"], self.base)
        self.assertEqual(j["state"], "building")
        self.assertEqual(j["rounds"], 0)

    def test_workspace_recovery_rejects_other_job_and_git_store(self):
        from masf.worker import saved_workspace

        outside = self.root / "outside"
        outside.mkdir()
        for path in (outside, self.worker.root, self.worker.root / "git"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                saved_workspace(self.worker.root, path)

    def test_resolve_base_conflict_and_reverify_new_commit(self):
        from masf.cli import resume

        self.changed = "README"
        original = self.agent
        advanced = False

        def advance(role, job, cfg, workspace, inputs, owner, **kwargs):
            nonlocal advanced
            result = original(role, job, cfg, workspace, inputs, owner, **kwargs)
            if role == "review" and not advanced:
                advanced = True
                (self.source / "README").write_text("new main")
                command(["git", "add", "."], cwd=self.source)
                command(
                    [
                        "git",
                        "-c",
                        "user.name=test",
                        "-c",
                        "user.email=test@example.org",
                        "commit",
                        "-m",
                        "advance",
                    ],
                    cwd=self.source,
                )
                self.base = command(["git", "rev-parse", "HEAD"], cwd=self.source)
            return result

        with patch.object(worker.providers, "invoke", advance):
            with self.assertRaisesRegex(
                RuntimeError, "Conflicto al actualizar la base en README"
            ):
                self.worker.process()
        previous = self.job["candidate"]
        self.assertEqual(self.job["base_conflict"]["paths"], ["README"])
        workspace = self.worker.checkout(previous)
        (workspace / "README").write_text("resolved")
        self.worker.git("add", "-A", "--", ".", workspace=workspace)
        tree = self.worker.git("write-tree")
        resolved = self.worker.git(
            "commit-tree", tree, "-p", previous, "-p", self.base, "-m", "resolve"
        )
        queue.save(self.worker.db, self.job, "blocked", retry="publishing")
        with (
            patch("masf.cli.api", self.api),
            patch("masf.cli.latest", return_value=self.base),
            patch.dict("os.environ", {"MASF_JOBS_DIR": str(self.root / "jobs")}),
        ):
            resume(
                self.worker.db,
                self.cfg,
                f"{self.repo['slug']}#1",
                resolved_commit=resolved,
            )
        restored = json.loads(
            self.worker.db.execute("SELECT data FROM jobs").fetchone()[0]
        )
        self.assertEqual(restored["state"], "checking")
        self.assertEqual(restored["rounds"], 1)
        self.assertNotIn("checks", restored)
        self.assertNotIn("review", restored)
        self.assertEqual(
            restored["base_resolutions"][0]["previous_candidate"], previous
        )
        self.worker.job = restored
        self.worker.process()
        self.assertEqual(restored["state"], "pr_open")
        self.assertEqual(restored["checks"]["sha"], resolved)
        self.assertEqual(restored["review"]["sha"], resolved)

    def test_resolved_commit_rejects_unrelated_and_invalid_commits(self):
        from masf.cli import resume

        self.deny = True
        with self.assertRaisesRegex(RuntimeError, "403"):
            self.worker.process()
        queue.save(self.worker.db, self.job, "blocked", retry="publishing")
        for value in ("HEAD", self.base):
            with (
                self.subTest(value=value),
                patch("masf.cli.api", self.api),
                patch("masf.cli.latest", return_value=self.base),
                patch.dict("os.environ", {"MASF_JOBS_DIR": str(self.root / "jobs")}),
            ):
                with self.assertRaises(ValueError):
                    resume(
                        self.worker.db,
                        self.cfg,
                        f"{self.repo['slug']}#1",
                        resolved_commit=value,
                    )
        stored = json.loads(
            self.worker.db.execute("SELECT data FROM jobs").fetchone()[0]
        )
        self.assertEqual(stored["state"], "blocked")
        self.assertEqual(stored["candidate"], self.job["candidate"])


class Leases(TemporaryTest):
    def test_expired_attempt_cannot_publish(self):
        db = queue.connect(self.root)
        job = {"repo_id": 1, "number": 1, "slug": "org/test"}
        self.assertTrue(queue.claim(db, job, "first", -1))
        self.assertTrue(queue.claim(db, job, "second", 90))
        self.assertFalse(queue.claim(db, job, "third", 90))
        self.assertEqual(queue.renew(db, job, "first", 90), 0)
        self.assertEqual(queue.renew(db, job, "second", 90), 1)
        with self.assertRaisesRegex(RuntimeError, "reserva"):
            queue.save(db, job | {"attempt": "first"}, "publishing")


class ProviderFailure(TemporaryTest):
    def test_process_failure_and_timeout(self):
        for provider in ("codex", "cursor", "claude"):
            self.repo["routing"]["plan"] = provider
            for failure in ("tiempo agotado", "fallo del proceso"):
                with self.subTest(provider=provider, failure=failure):
                    with patch(
                        "masf.providers.command",
                        side_effect=["cli 1.0", RuntimeError(failure), ""],
                    ) as run:
                        with self.assertRaisesRegex(RuntimeError, failure):
                            providers.invoke(
                                "plan",
                                {"repo": self.repo},
                                self.cfg,
                                self.root,
                                {},
                                "test-owner",
                            )
                        self.assertEqual(
                            run.call_args.args[0][:3], ["docker", "rm", "-f"]
                        )
