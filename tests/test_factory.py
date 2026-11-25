import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from masf import config, providers, queue, worker
from masf.system import ProcessError, command

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
            CONFIG.read_text().replace(
                "enabled = false\nlabel", "enabled = true\nlabel"
            )
        )
        profile = self.root / "profiles/postgres.yaml"
        profile.parent.mkdir()
        profile.write_bytes((CONFIG.parent / "profiles/postgres.yaml").read_bytes())
        self.scan_argv = [
            sys.executable,
            "-m",
            "masf.cli",
            "--config",
            str(config_file),
            "scan",
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
            self.issue_queries.extend(
                [endpoint, f"{issues}&page=1", f"{issues}&page=2"]
            )
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
        return subprocess.run([*self.scan_argv, *args], timeout=30, **self.scan_options)

    def jobs(self):
        database = self.state_home / "masf/queue.sqlite3"
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
            return db.execute(
                "SELECT repo_id, number, slug, state FROM jobs"
            ).fetchall()

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
        self.addCleanup(db.close)
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

    def agent(
        self, role, job, cfg, workspace, inputs, owner, *, guard=None, on_activity=None
    ):
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

    def api(self, endpoint, *opts, stdin=None):
        if stdin is not None:
            return json.loads(
                worker.command(["gh", "api", endpoint, *opts], stdin=stdin)
            )
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

    def test_distinct_providers_exchange_persisted_plan_and_checked_diff(self):
        remote = self.root / "remote.git"
        command(["git", "clone", "--bare", str(self.source), str(remote)])
        self.worker.url = str(remote)
        self.repo["routing"].update(plan="claude", build="codex", review="cursor")
        plan = {"version": 1, "summary": "Add a greeting", "criteria": ["AC1 Say hello"]}
        check_argv = [
            sys.executable, "-c",
            "from pathlib import Path; print(Path('greeting').read_text().strip())",
        ]
        checked = {}
        reviewed = []

        def plan_with_claude(workspace, inputs):
            self.assertEqual(inputs["sha"], self.base)
            return plan

        def build_with_codex(workspace, inputs):
            self.assertEqual(inputs["plan"], plan)
            (workspace / "greeting").write_text("Hello\n")
            return {"version": 1, "summary": "Added greeting", "changes": ["AC1 Say hello"]}

        def review_with_cursor(workspace, inputs):
            stored = json.loads(self.worker.db.execute("SELECT data FROM jobs").fetchone()[0])
            sha = inputs["sha"]
            self.assertNotEqual(sha, self.base)
            self.assertEqual(sha, stored["candidate"])
            self.assertEqual(inputs["plan"], stored["plan"])
            self.assertEqual(inputs["plan"], plan)
            self.assertEqual(inputs["checks"], checked[sha])
            self.assertEqual(inputs["checks"], stored["checks"])
            self.assertEqual(inputs["checks"]["sha"], sha)
            self.assertEqual((workspace / "greeting").read_text(), "Hello\n")
            reviewed.append(inputs)
            return {"version": 1, "summary": "Reviewed", "verdict": "pass", "findings": []}

        adapters = {"claude": plan_with_claude, "codex": build_with_codex, "cursor": review_with_cursor}

        def invoke(role, job, cfg, workspace, inputs, owner, **kwargs):
            provider = job["repo"]["routing"][role]
            return {"provider": provider, "result": adapters[provider](workspace, inputs)}

        def check(sha):
            if sha == self.base:
                return self.check(sha)
            self.assertNotIn(sha, checked)
            workspace = self.worker.checkout(sha)
            log = command(check_argv, cwd=workspace)
            self.assertEqual(log, "Hello")
            report = {"sha": sha, "passed": True, "commands": [
                {"argv": check_argv, "code": 0, "log": log},
            ]}
            checked[sha] = report
            return report

        with patch.object(worker.providers, "invoke", invoke), patch.object(self.worker, "test", check):
            self.worker.process()
        (review,) = reviewed
        sha = self.prs[0]["head"]["sha"]
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(review["sha"], sha)
        self.assertEqual(self.job["review"]["provider"], "cursor")
        self.assertEqual(review["diff"], command(
            ["git", "--git-dir", str(remote), "diff", self.base, sha]
        ))
        self.assertIn("+Hello", review["diff"])

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

        self.cfg["service"]["max_recovery_attempts"] = 0
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
                RuntimeError, "Recuperaciones automáticas agotadas"
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
        self.job = restored
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

    def run_controller(self):
        from masf import cli

        previous = self.worker

        def create(db, cfg, config, state, jobs, job):
            self.job = job
            self.worker = worker.Worker(db, cfg, config, state, jobs, job)
            self.worker.url = str(self.source)
            self.worker.test = previous.test
            self.worker.recover = previous.recover
            return self.worker

        due = max(time.time(), self.job.get("recovery_wait", {}).get("until", 0)) + 1
        with (
            patch.dict("os.environ", {"MASF_JOBS_DIR": str(self.root / "jobs")}),
            patch.object(cli, "Worker", side_effect=create),
            patch("masf.cli.time.time", return_value=due),
        ):
            result = cli.process_task(self.cfg, CONFIG, self.root / "state", self.job)
        self.worker.db = previous.db
        self.job = json.loads(
            self.worker.db.execute("SELECT data FROM jobs").fetchone()[0]
        )
        self.worker.job = self.job
        return result

    def test_timeout_recovers_workspace_after_restart_with_exhausted_build_rounds(self):
        from masf.system import CommandTimeout

        self.cfg["service"]["max_build_rounds"] = 1
        original = self.agent
        seen = []

        def interrupted(role, job, cfg, workspace, inputs, owner, **kwargs):
            if role == "build":
                seen.append(workspace)
                if len(seen) == 1:
                    (workspace / "partial").write_text("keep")
                    raise CommandTimeout("docker: tiempo agotado (900s)")
                self.assertEqual((workspace / "partial").read_text(), "keep")
                self.assertTrue(inputs["recovery"]["workspace_reused"])
            return original(role, job, cfg, workspace, inputs, owner, **kwargs)

        with patch.object(worker.providers, "invoke", interrupted):
            self.assertEqual(self.run_controller(), 1)
            self.assertEqual(self.job["state"], "retry_wait")
            self.assertGreater(self.job["recovery_wait"]["until"], time.time())
            # Reload from SQLite as a fresh controller would after restarting.
            self.assertEqual(self.run_controller(), 0)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(len(self.job["recoveries"]), 1)

    def test_review_recovery_keeps_sha_checks_and_build(self):
        from masf.recovery import RecoverableFailure

        original = self.agent
        failed = False

        def interrupted(role, *args, **kwargs):
            nonlocal failed
            if role == "review" and not failed:
                failed = True
                raise RecoverableFailure("Cursor: flujo incompleto")
            return original(role, *args, **kwargs)

        with patch.object(worker.providers, "invoke", interrupted):
            self.run_controller()
            sha = self.job["candidate"]
            checks = self.job["checks"]
            self.assertEqual(self.job["state"], "retry_wait")
            self.run_controller()
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(self.job["candidate"], sha)
        self.assertEqual(self.job["checks"], checks)
        self.assertEqual(self.roles.count("build"), 1)

    def test_build_access_failure_keeps_job_and_workspace_for_operator_resume(self):
        from masf.cli import resume

        self.repo["routing"]["build"] = "claude"
        self.worker.update("queued")
        workspaces = []

        def agent(role, job, cfg, workspace, inputs, owner, **kwargs):
            if role == "build":
                workspaces.append(workspace)
                partial = workspace / "partial"
                if len(workspaces) == 1:
                    partial.write_text("keep")
                    raise ProcessError("HTTP 401 Unauthorized, token=synthetic-value")
                self.assertEqual(partial.read_text(), "keep")
                self.assertTrue(inputs["recovery"]["workspace_reused"])
                self.assertEqual(inputs["recovery"]["previous_error"], cause)
            return self.agent(role, job, cfg, workspace, inputs, owner, **kwargs)

        cause = "HTTP 401 Unauthorized, token=[redacted]"
        identity = (self.job["repo_id"], self.job["number"], self.job["fingerprint"])
        with patch.object(worker.providers, "invoke", agent):
            self.assertEqual(self.run_controller(), 1)
            with closing(queue.connect(self.root / "state")) as db:
                blocked = json.loads(db.execute("SELECT data FROM jobs").fetchone()[0])
                self.assertEqual((blocked["repo_id"], blocked["number"], blocked["fingerprint"]), identity)
                self.assertEqual(blocked["state"], "blocked")
                self.assertEqual(blocked["retry"], "building")
                self.assertEqual(blocked["reason"], cause)
                self.assertEqual(blocked["build_workspace"]["error"], cause)
                self.assertEqual(blocked["build_workspace"]["path"], str(workspaces[0]))
                (evidence,) = self.worker.evidence.glob("build-*.json")
                details = json.loads(evidence.read_text())
                self.assertEqual(details["provider"], blocked["repo"]["routing"]["build"])
                self.assertEqual(details["provider"], "claude")
                self.assertEqual(details["error"], cause)
                self.assertEqual(details["role"], "build")
                with patch("masf.cli.api", self.api):
                    resume(db, self.cfg, f"{blocked['slug']}#{blocked['number']}")
            self.assertEqual(self.run_controller(), 0)
        self.assertEqual(workspaces, [workspaces[0], workspaces[0]])
        self.assertEqual((self.job["repo_id"], self.job["number"], self.job["fingerprint"]), identity)
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(self.roles, ["plan", "build", "review"])

    def test_review_quota_failure_keeps_job_candidate_and_checks_for_retry(self):
        self.repo["routing"]["review"] = "cursor"
        reviews = []

        def agent(role, job, cfg, workspace, inputs, owner, **kwargs):
            if role == "review":
                reviews.append(inputs)
                if len(reviews) == 1:
                    raise ProcessError("HTTP 429 quota exhausted, api_key=synthetic-value")
            return self.agent(role, job, cfg, workspace, inputs, owner, **kwargs)

        cause = "HTTP 429 quota exhausted, api_key=[redacted]"
        identity = (self.job["repo_id"], self.job["number"], self.job["fingerprint"])
        with (
            patch.object(worker.providers, "invoke", agent),
            patch.object(self.worker, "test", wraps=self.check) as check,
        ):
            self.assertEqual(self.run_controller(), 1)
            with closing(queue.connect(self.root / "state")) as db:
                waiting = json.loads(db.execute("SELECT data FROM jobs").fetchone()[0])
            self.assertEqual((waiting["repo_id"], waiting["number"], waiting["fingerprint"]), identity)
            self.assertEqual(waiting["state"], "retry_wait")
            self.assertEqual(waiting["recovery_wait"]["stage"], "reviewing")
            self.assertEqual(waiting["reason"], cause)
            self.assertEqual(waiting["recoveries"][-1]["reason"], cause)
            (evidence,) = self.worker.evidence.glob("review-*.json")
            details = json.loads(evidence.read_text())
            self.assertEqual(details["provider"], waiting["repo"]["routing"]["review"])
            self.assertEqual(details["provider"], "cursor")
            self.assertEqual(details["error"], cause)
            self.assertEqual(details["role"], "review")
            self.assertEqual(details["sha"], waiting["candidate"])
            self.assertEqual(waiting["checks"], reviews[0]["checks"])
            self.assertEqual(self.run_controller(), 0)
            self.assertEqual([call.args[0] for call in check.call_args_list], [self.base, waiting["candidate"]])
        self.assertEqual((self.job["repo_id"], self.job["number"], self.job["fingerprint"]), identity)
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(self.job["candidate"], waiting["candidate"])
        self.assertEqual(self.job["checks"], waiting["checks"])
        self.assertEqual(reviews, [reviews[0], reviews[0]])
        self.assertEqual(self.roles, ["plan", "build", "review"])

    def test_repeated_timeout_stops_at_durable_budget(self):
        from masf.system import CommandTimeout

        with patch.object(
            worker.providers, "invoke", side_effect=CommandTimeout("timeout")
        ):
            for _ in range(3):
                self.run_controller()
        self.assertEqual(self.job["state"], "blocked")
        self.assertEqual(len(self.job["recoveries"]), 2)
        self.assertIn("presupuesto", self.job["reason"])
        self.assertFalse(self.prs)

    def test_lost_publication_response_recovers_one_pr_automatically(self):
        from masf.system import ProcessError

        original = self.run_command
        lost = False

        def request(args, **kwargs):
            nonlocal lost
            result = original(args, **kwargs)
            if args[0] == "gh" and not lost:
                lost = True
                raise ProcessError("gh: HTTP 502")
            return result

        with patch.object(worker, "command", request):
            self.run_controller()
            self.assertEqual(self.job["state"], "retry_wait")
            self.run_controller()
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(self.roles, ["plan", "build", "review"])

    def test_issue_closed_during_wait_cancels_without_retrying_provider(self):
        from masf.system import CommandTimeout

        with patch.object(
            worker.providers, "invoke", side_effect=CommandTimeout("timeout")
        ):
            self.run_controller()
        self.issue["state"] = "closed"
        with patch.object(worker.providers, "invoke") as agent:
            self.run_controller()
            agent.assert_not_called()
        self.assertEqual(self.job["state"], "cancelled")

    def test_interrupted_recovery_build_cannot_bypass_its_budget(self):
        self.cfg["service"]["max_recovery_attempts"] = 1
        original = self.agent

        def interrupted(role, *args, **kwargs):
            if role == "build":
                raise KeyboardInterrupt()
            return original(role, *args, **kwargs)

        with patch.object(worker.providers, "invoke", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.process()
            # A restart may reserve one recovery attempt, but another restart cannot renew it.
            with self.assertRaises(KeyboardInterrupt):
                self.worker.process()
            with self.assertRaisesRegex(
                RuntimeError, "Recuperaciones automáticas agotadas"
            ):
                self.worker.process()
        self.assertEqual(len(self.job["recoveries"]), 1)
        self.assertFalse(self.prs)

    def test_auth_and_protected_paths_require_operator(self):
        from masf.cli import resume
        from masf.system import ProcessError

        with patch.object(
            worker.providers,
            "invoke",
            side_effect=ProcessError("HTTP 401 Unauthorized"),
        ):
            self.run_controller()
        self.assertEqual(self.job["state"], "blocked")
        self.assertFalse(self.job.get("recoveries"))
        self.changed = ".github/workflows/ci.yml"
        with patch("masf.cli.api", self.api):
            resume(self.worker.db, self.cfg, f"{self.repo['slug']}#1")
        self.run_controller()
        self.assertEqual(self.job["state"], "blocked")
        self.assertIn("Ruta protegida", self.job["reason"])
        self.assertFalse(self.job.get("recoveries"))

    def test_base_conflict_is_resolved_by_builder_then_rechecked_and_reviewed(self):
        self.changed = "README"
        self.cfg["service"]["max_build_rounds"] = 1
        original = self.agent
        advanced = False
        previous = None
        checks = []
        original_check = self.check

        def check(sha):
            checks.append(sha)
            return original_check(sha)

        def agent(role, job, cfg, workspace, inputs, owner, **kwargs):
            nonlocal advanced, previous
            recovery = inputs.get("recovery", {})
            if role == "build" and recovery.get("type") == "base_conflict":
                self.assertIn("<<<<<<<", (workspace / "README").read_text())
                self.assertEqual(recovery["paths"], ["README"])
            result = original(role, job, cfg, workspace, inputs, owner, **kwargs)
            if role == "review" and not advanced:
                advanced = True
                previous = job["candidate"]
                (self.source / "README").write_text("advanced main\n")
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

        with (
            patch.object(worker.providers, "invoke", agent),
            patch.object(self.worker, "test", check),
        ):
            self.assertEqual(self.run_controller(), 0)
        self.assertEqual(self.job["state"], "pr_open")
        self.assertEqual(self.roles, ["plan", "build", "review", "build", "review"])
        self.assertIn(self.base, checks)
        self.assertIn(self.job["candidate"], checks)
        self.assertEqual(self.job["checks"]["sha"], self.job["review"]["sha"])
        for ancestor in (previous, self.base):
            self.worker.git(
                "merge-base", "--is-ancestor", ancestor, self.job["candidate"]
            )
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(self.job["recoveries"][0]["action"], "resolve_base")


    def open_ci_pr(self):
        from masf import cli

        self.worker.process()
        self.old_head = self.job["candidate"]
        self.action_attempt = 1
        self.action_state = "completed"
        self.action_result = "failure"
        self.action_metadata = {}
        self.annotation = "FAIL: test_check_interrupted (tests.Checks) token ghp_secret123"
        self.ci_logs = None
        self.prs[0]["head"].update(ref="masf/issue-1")
        self.prs[0]["head"]["repo"]["id"] = 123
        self.prs[0]["base"] = {"ref": "main", "repo": {"id": 123}}
        self.pr_lookups = []
        self.feedback = []
        self.checks_seen = []
        self.fail_push_response = None
        self.extra_check_pending = False

        def lookup(endpoint, *, raw=False):
            if endpoint.endswith("/pulls/3"):
                self.pr_lookups.append(3)
                return json.loads(json.dumps(self.prs[0]))
            if "/git/ref/heads/" in endpoint:
                return {"object": {"sha": self.worker.remote_head()}}
            if "/actions/runs?" in endpoint:
                sha = endpoint.split("head_sha=")[1].split("&")[0]
                different = sha != self.old_head
                run = dict(head_sha=sha, head_branch="masf/issue-1",
                           id=9 if different else 7,
                           run_attempt=1 if different else self.action_attempt,
                           status="completed" if different else self.action_state,
                           conclusion="success" if different else self.action_result)
                run.update(self.action_metadata)
                return {"workflow_runs": [run, run | {"head_sha": self.base}]}
            if "/attempts/" in endpoint:
                different = "/runs/9/" in endpoint
                item = dict(id=90 if different else 70 + self.action_attempt,
                            name="unit tests", status="completed",
                            conclusion="success" if different else self.action_result,
                            check_run_url="https://test.invalid/check-runs/42",
                            steps=[dict(name="Run unittest", conclusion="failure")])
                return {"jobs": [item]}
            if "/check-runs?" in endpoint:
                sha = endpoint.split("/commits/")[1].split("/")[0]
                items = [dict(id=99, name="external", head_sha=sha, status="in_progress")] if self.extra_check_pending else []
                return {"check_runs": items}
            if "/statuses?" in endpoint:
                return []
            if "/annotations?" in endpoint:
                return [dict(message=self.annotation)]
            if endpoint.endswith("/logs"):
                self.assertTrue(raw)
                return self.ci_logs if self.ci_logs is not None else "setup complete\n" + self.annotation
            if "/issues/1" in endpoint:
                return self.issue
            if endpoint == f"repos/{self.job['slug']}":
                return {"id": 123, "default_branch": "main"}
            if "/commits/main" in endpoint:
                return {"sha": self.base}
            self.fail(f"Unexpected endpoint {endpoint}")

        def git_or_gh(args, **kwargs):
            if args[0] == "gh":
                self.fail("Repair would create another PR")
            result = command(args, **kwargs)
            if "push" in args and args[0] == "git":
                self.prs[0]["head"]["sha"] = self.worker.remote_head()
                if self.fail_push_response:
                    raise self.fail_push_response("simulated death after push")
            return result

        def agent(role, job, cfg, workspace, inputs, owner, *, guard=None, on_activity=None):
            if role == "build":
                self.feedback.append(inputs)
            return self.agent(role, job, cfg, workspace, inputs, owner, guard=guard, on_activity=on_activity)

        def check(sha):
            self.checks_seen.append(sha)
            return self.check(sha)

        def create_worker(db, cfg, config_file, state, jobs, job):
            instance = worker.Worker(db, cfg, config_file, state, self.root / "jobs", job)
            instance.url = self.worker.url
            instance.recover = lambda: None
            instance.test = check
            return instance

        for target, name, value in [
            (cli, "api", lookup), (worker, "api", lookup),
            (cli, "scan", lambda *_, **__: 0), (cli, "observe", lambda *_: 0),
            (cli, "Worker", create_worker),
            (worker, "command", git_or_gh), (providers, "invoke", agent),
        ]:
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = f"{self.job['slug']}#1"

    def ci_job(self):
        return json.loads(self.worker.db.execute("SELECT data FROM jobs").fetchone()[0])

    def ci_tick(self, *, observe_only=False):
        from masf import cli

        with ExitStack() as stack:
            if observe_only:
                stack.enter_context(patch.object(cli, "process_task", return_value=0))
            return cli.tick(self.worker.db, self.cfg, CONFIG, self.root / "state", True)

    def test_ci_repair_delivers_and_observes_same_pr(self):
        self.open_ci_pr()
        self.assertEqual(self.ci_tick(), 0)
        repaired = self.ci_job()
        sha = repaired["candidate"]
        self.assertEqual(repaired["state"], "pr_open")
        self.assertNotEqual(sha, self.old_head)
        self.assertEqual(self.worker.remote_head(), sha)
        self.assertEqual(repaired["pr_number"], 3)
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(self.roles, ["plan", "build", "review", "build", "review"])
        self.assertEqual(self.checks_seen, [sha])
        self.assertEqual(repaired["review"]["sha"], sha)
        self.assertEqual(repaired["checks"]["sha"], sha)
        self.assertEqual(repaired["ci_rounds"], 1)
        feedback = self.feedback[0]
        self.assertEqual(feedback["sha"], self.old_head)
        self.assertEqual(feedback["plan"]["criteria"], ["AC1 greet"])
        failure = feedback["feedback"]["diagnostics"][0]
        self.assertEqual((failure["check"]["run"], failure["check"]["attempt"]), (7, 1))
        self.assertIn("test_check_interrupted", failure["evidence"])
        self.assertNotIn("secret123", failure["evidence"])
        self.assertEqual(self.ci_tick(), 0)
        self.assertEqual(self.ci_job()["ci"]["state"], "success")
        self.assertEqual(self.ci_job()["ci"]["sha"], sha)
        history = [json.loads(row[0]) for row in self.worker.db.execute("SELECT detail FROM events")]
        self.assertTrue(any(event.get("ci", {}).get("sha") == self.old_head
                            and event["ci"]["state"] == "failed" for event in history))
        self.assertTrue(any(event.get("review", {}).get("sha") == self.old_head for event in history))

        build, = [item for item in queue.stage_attempts(self.worker.db, repaired, role="build")
                  if item["ci_origin"]]
        self.assertEqual(build["ci_origin"]["sha"], self.old_head)
        self.assertEqual(build["input_sha"], self.old_head)
        self.assertEqual(build["ci_result"], {"pr": 3, "sha": sha})
        self.assertEqual(build["candidate_sha"], sha)
        self.assertEqual(build["model"]["availability"], "unknown")
        decision, = [event["ci_repair"] for event in history if event.get("ci_repair")]
        self.assertEqual(build["ci_origin"]["id"], decision["id"])
        self.assertEqual(decision["diagnostics"][0]["kind"], "functional")
        self.assertIn("plan vigente", decision["decision"])
        self.assertEqual(self.ci_job()["ci"]["evidence"], build["ci_result"])
        published = queue.stage_attempts(self.worker.db, repaired, role="publish")[-1]
        self.assertEqual(published["result"]["intent"]["target"], sha)
        self.assertEqual(published["result"]["pr"], 3)

    def test_published_summary_filters_role_results_and_commands(self):
        original = self.agent

        def agent(role, *args, **kwargs):
            response = original(role, *args, **kwargs)
            if role == "review":
                response["result"]["summary"] = "Aprobado, api_key=review-fixture"
            elif role == "plan":
                response["result"]["criteria"] = ["AC1 token=plan-fixture"]
            return response

        def checks(sha):
            report = self.check(sha)
            report["commands"][0]["argv"] += ["--password", "command-fixture"]
            return report

        with (
            patch.object(providers, "invoke", agent),
            patch.object(self.worker, "test", checks),
        ):
            self.worker.process()
        raw = self.prs[0]["body"]
        self.assertIn("token=[redacted]", raw)
        self.assertIn("api_key=[redacted]", raw)
        self.assertIn("--password [redacted]", raw)
        for secret in ("review-fixture", "plan-fixture", "command-fixture"):
            self.assertNotIn(secret, raw)
        self.assertIn(self.job["candidate"], raw)

    def test_historical_publication_resumes_with_original_plan_candidate_and_checks(self):
        from masf import cli

        self.deny = True
        self.assertEqual(self.run_controller(), 1)
        historical = self.job
        historical.pop("plan_attempt", None)
        historical["review"].pop("stage_attempt", None)
        historical["build"].pop("stage_attempt", None)
        candidate, report, plan = (historical[key] for key in ("candidate", "checks", "plan"))
        with self.worker.db:
            self.worker.db.execute("DELETE FROM stage_attempts")
            self.worker.db.execute("UPDATE jobs SET data=?", (json.dumps(historical),))
        evidence = cli.job_evidence(self.worker.db, historical, self.root / "state")
        self.assertIsNone(evidence["provenance"]["engine"])
        review, = [item for item in evidence["stage_attempts"] if item["role"] == "review"]
        self.assertTrue(review["historical"])
        self.assertEqual(review["input_sha"], candidate)
        self.deny = False
        with patch.object(cli, "api", self.api):
            cli.resume(self.worker.db, self.cfg, f"{historical['slug']}#1")
        self.assertEqual(self.run_controller(), 0)
        self.assertEqual(self.roles, ["plan", "build", "review"])
        self.assertEqual(self.job["candidate"], candidate)
        self.assertEqual(self.job["checks"], report)
        self.assertEqual(self.job["plan"], plan)
        publication = queue.stage_attempts(self.worker.db, self.job, role="publish")[-1]
        self.assertEqual(publication["result"]["sha"], candidate)
        self.assertEqual(publication["model"]["availability"], "not_applicable")

    def test_ci_late_metadata_and_masked_logs_preserve_the_original_route(self):
        from masf import ci, cli

        self.open_ci_pr()
        self.annotation = "Test failed"
        self.ci_logs = ("::add-mask::fixture-value\n"
                        "AssertionError: observed fixture-value during test")
        self.assertEqual(self.ci_tick(observe_only=True), 0)
        routed = self.ci_job()
        self.assertEqual(routed["state"], "building")
        repair = routed["ci_repair"]
        self.assertNotIn("fixture-value", json.dumps(repair))
        self.assertIn("[redacted]", repair["diagnostics"][0]["evidence"])

        self.action_metadata = {"run_started_at": "2026-11-24T12:00:00Z",
                                "html_url": "https://github.com/org/repo/actions/runs/7"}
        with closing(queue.connect(self.root / "state")) as reopened:
            self.assertTrue(ci.observe_and_route(reopened, routed, self.cfg, cli.api, revalidate=True))
            self.assertEqual(routed["ci"]["workflows"][0]["run_started_at"], self.action_metadata["run_started_at"])
            self.assertEqual(routed["ci_repair"], repair)
            count = reopened.execute("SELECT count(*) FROM events").fetchone()[0]
            self.assertTrue(ci.observe_and_route(reopened, routed, self.cfg, cli.api, revalidate=True))
            self.assertEqual(reopened.execute("SELECT count(*) FROM events").fetchone()[0], count)

    def test_ci_pending_duplicate_and_rerun(self):
        self.open_ci_pr()
        self.action_state = "in_progress"
        self.assertEqual(self.ci_tick(), 0)
        self.assertEqual(self.ci_job()["ci"]["state"], "pending")
        self.action_state = "completed"
        self.extra_check_pending = True
        self.ci_tick()
        self.assertEqual(self.ci_job()["ci"]["state"], "pending")
        self.extra_check_pending = False
        self.ci_tick(observe_only=True)
        self.assertEqual(self.ci_job()["state"], "building")
        events = self.worker.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.ci_tick(observe_only=True)
        self.assertEqual(self.worker.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.action_attempt = 2
        self.action_state = "queued"
        self.ci_tick()
        self.assertEqual(self.ci_job()["ci"]["pending_runs"], [[7, 2]])
        self.action_state = "completed"
        self.action_result = "success"
        self.ci_tick()
        self.assertEqual(self.ci_job()["ci"]["state"], "success")
        self.assertEqual(self.ci_job()["rounds"], 1)
        self.assertEqual(self.roles, ["plan", "build", "review"])
        self.assertEqual(self.worker.remote_head(), self.old_head)

    def test_ci_infrastructure_and_unknown_need_intervention(self):
        from masf import cli

        self.open_ci_pr()
        self.cfg["service"]["ci_retry_seconds"] = 0
        self.cfg["service"]["max_ci_technical_retries"] = 1
        self.annotation = "Runner unavailable"
        self.ci_tick()
        job = self.ci_job()
        self.assertEqual(job["state"], "needs_input")
        self.assertEqual(job["ci_technical"], 1)
        self.assertEqual(job["rounds"], 1)
        self.assertEqual(self.checks_seen, [self.old_head])
        self.assertEqual(self.feedback, [])
        cli.resume(self.worker.db, self.cfg, self.target)
        self.ci_tick()
        self.assertEqual(self.ci_job()["state"], "pr_open")
        self.action_attempt = 2
        self.ci_tick()
        self.assertIn("agotados", self.ci_job()["reason"])
        self.assertEqual(self.ci_job()["ci_technical"], 1)
        cli.resume(self.worker.db, self.cfg, self.target)
        self.action_attempt = 3
        self.annotation = "step failed"
        self.ci_tick()
        self.assertIn("insuficiente", self.ci_job()["reason"])
        self.assertEqual(self.worker.remote_head(), self.old_head)

    def test_ci_third_party_head_and_exhausted_rounds_require_approval(self):
        from masf import cli

        self.open_ci_pr()
        self.cfg["service"]["max_build_rounds"] = 1
        self.ci_tick()
        self.assertIn("Rondas agotadas", self.ci_job()["reason"])
        command(["git", "branch", "-f", "masf/issue-1", self.base], cwd=self.source)
        self.prs[0]["head"]["sha"] = self.base
        self.ci_tick()
        self.assertEqual(self.ci_job()["head_conflict"]["observed"], self.base)
        with self.assertRaises(ValueError):
            cli.resume(self.worker.db, self.cfg, self.target, accept_head=self.old_head, evidence="Reviewed")
        self.assertEqual(self.worker.remote_head(), self.base)
        self.old_head = self.base
        cli.resume(self.worker.db, self.cfg, self.target, accept_head=self.base,
                   add_rounds=1, evidence="Reviewed the new head and approved one round")
        self.assertEqual(self.ci_job()["rounds"], 1)
        self.assertEqual([entry["kind"] for entry in self.ci_job()["decisions"]], ["head", "rounds"])
        self.ci_tick()
        self.assertEqual(self.ci_job()["state"], "pr_open")
        self.assertEqual(self.ci_job()["rounds"], 2)
        self.assertNotEqual(self.worker.remote_head(), self.base)

    def test_ci_protected_paths_and_lost_push_response(self):
        from masf import cli

        self.open_ci_pr()
        self.changed = ".github/workflows/factory.yml"
        self.assertEqual(self.ci_tick(), 1)
        blocked = self.ci_job()
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(self.worker.remote_head(), self.old_head)
        cli.resume(self.worker.db, self.cfg, self.target, [self.changed])
        self.fail_push_response = RuntimeError
        self.assertEqual(self.ci_tick(), 1)
        job = self.ci_job()
        self.assertEqual(job["repair_intent"]["expected"], self.old_head)
        self.assertEqual(job["repair_intent"]["target"], self.worker.remote_head())
        cli.resume(self.worker.db, self.cfg, self.target)
        self.fail_push_response = False
        self.assertEqual(self.ci_tick(), 0)
        self.assertEqual(self.ci_job()["state"], "pr_open")
        self.assertEqual(self.ci_job()["ci_rounds"], 1)
        self.assertEqual(self.roles.count("build"), 2)
        self.assertEqual(len(self.prs), 1)

    def test_ci_preserves_third_party_push_before_publication(self):
        self.open_ci_pr()
        run_agent = providers.invoke

        def update_remote_after_review(*args, **kwargs):
            result = run_agent(*args, **kwargs)
            if args[0] == "review":
                command(["git", "branch", "-f", "masf/issue-1", self.base], cwd=self.source)
                self.prs[0]["head"]["sha"] = self.base
            return result

        with patch.object(providers, "invoke", update_remote_after_review):
            self.assertEqual(self.ci_tick(), 0)
        job = self.ci_job()
        self.assertEqual(job["state"], "needs_input")
        self.assertEqual(job["head_conflict"]["expected"], self.old_head)
        self.assertEqual(job["review"]["sha"], job["candidate"])
        self.assertEqual(self.worker.remote_head(), self.base)
        self.assertIsNone(job.get("repair_intent"))

    def test_ci_timeout_recovers_workspace_without_spending_another_round(self):
        from masf.system import CommandTimeout

        self.open_ci_pr()
        original = providers.invoke
        workspaces = []

        def interrupted(role, job, cfg, workspace, inputs, owner, **kwargs):
            if role == "build":
                workspaces.append(workspace)
                if len(workspaces) == 1:
                    (workspace / "partial").write_text("keep")
                    raise CommandTimeout("build timeout")
                self.assertEqual((workspace / "partial").read_text(), "keep")
            return original(role, job, cfg, workspace, inputs, owner, **kwargs)

        with patch.object(providers, "invoke", interrupted):
            self.assertEqual(self.ci_tick(), 1)
            waiting = self.ci_job()
            self.assertEqual(waiting["state"], "retry_wait")
            self.assertEqual(self.ci_tick(), 0)
            self.assertEqual(len(workspaces), 1)
            with patch("masf.cli.time.time", return_value=waiting["recovery_wait"]["until"] + 1):
                self.assertEqual(self.ci_tick(), 0)
        repaired = self.ci_job()
        self.assertEqual(repaired["state"], "pr_open")
        self.assertEqual(repaired["rounds"], waiting["rounds"])
        self.assertEqual(repaired["ci_rounds"], 1)
        self.assertEqual(workspaces[0], workspaces[1])
        self.assertEqual(len(repaired["recoveries"]), 1)
        self.assertEqual(len(self.prs), 1)

    def test_ci_interrupted_build_recovery_is_bounded_across_restarts(self):
        self.open_ci_pr()
        self.cfg["service"]["max_recovery_attempts"] = 1
        original = providers.invoke

        def interrupted(role, *args, **kwargs):
            if role == "build":
                raise KeyboardInterrupt()
            return original(role, *args, **kwargs)

        with patch.object(providers, "invoke", interrupted):
            for _ in range(2):
                with self.assertRaises(KeyboardInterrupt):
                    self.ci_tick()
            self.assertEqual(self.ci_tick(), 1)
        stopped = self.ci_job()
        self.assertEqual(stopped["state"], "blocked")
        self.assertEqual(len(stopped["recoveries"]), 1)
        self.assertEqual(stopped["ci_rounds"], 1)
        self.assertEqual(self.worker.remote_head(), self.old_head)

    def test_ci_base_conflict_preserves_current_base_and_previous_candidate(self):
        self.open_ci_pr()
        self.changed = "README"
        original = providers.invoke
        previous = None

        def advance(role, job, cfg, workspace, inputs, owner, **kwargs):
            nonlocal previous
            result = original(role, job, cfg, workspace, inputs, owner, **kwargs)
            if role == "review" and previous is None:
                previous = job["candidate"]
                (self.source / "README").write_text("advanced main\n")
                command(["git", "add", "."], cwd=self.source)
                command(["git", "-c", "user.name=test", "-c", "user.email=test@example.org",
                         "commit", "-m", "advance"], cwd=self.source)
                self.base = command(["git", "rev-parse", "HEAD"], cwd=self.source)
            return result

        with patch.object(providers, "invoke", advance):
            self.assertEqual(self.ci_tick(), 0)
        repaired = self.ci_job()
        self.assertEqual(repaired["state"], "pr_open")
        self.assertEqual(repaired["recoveries"][0]["action"], "resolve_base")
        for ancestor in (previous, self.base):
            self.worker.git("merge-base", "--is-ancestor", ancestor, repaired["candidate"])
        self.assertEqual(repaired["checks"]["sha"], repaired["review"]["sha"])
        self.assertEqual(self.worker.remote_head(), repaired["candidate"])
        self.assertEqual(len(self.prs), 1)

    def test_ci_restart_after_build_result_reuses_round(self):
        self.open_ci_pr()
        original = worker.Worker.git

        def crash_after_build(instance, *args, **kwargs):
            if args[0] == "add" and (instance.job.get("build_workspace") or {}).get("result"):
                raise KeyboardInterrupt("simulated abrupt exit")
            return original(instance, *args, **kwargs)

        with patch.object(worker.Worker, "git", crash_after_build):
            with self.assertRaises(KeyboardInterrupt):
                self.ci_tick()
        checkpoint = self.ci_job()["build_workspace"]
        self.assertIn("result", checkpoint)
        self.assertEqual(self.worker.remote_head(), self.old_head)
        self.assertEqual(self.ci_tick(), 0)
        self.assertEqual(self.ci_job()["state"], "pr_open")
        self.assertEqual(self.ci_job()["ci_rounds"], 1)
        self.assertEqual(self.roles.count("build"), 2)

    def test_ci_restart_after_push_waits_for_confirmation(self):
        self.open_ci_pr()
        self.fail_push_response = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.ci_tick()
        pushed = self.worker.remote_head()
        self.assertEqual(self.ci_job()["state"], "publishing")
        self.assertNotEqual(pushed, self.old_head)
        self.prs[0]["head"]["sha"] = self.old_head
        self.ci_tick()
        self.assertEqual(self.ci_job()["state"], "publishing")
        self.prs[0]["head"]["sha"] = pushed
        self.ci_tick()
        self.assertEqual(self.ci_job()["published_head"], pushed)
        self.assertEqual(self.ci_job()["state"], "pr_open")
        self.assertEqual(self.roles.count("build"), 2)

    def test_ci_build_timings_and_passing_tests_do_not_hide_assertion(self):
        self.open_ci_pr()
        self.annotation = "Process completed with exit code 1."
        self.ci_logs = "\n".join([
            "test_failed_pr_lookup_does_not_stop_other_jobs ... org/repo: 403",
            "test_ci_access_quota_and_configuration ... ok",
            "test_retry_after_is_respected ... ok",
            "#10 4.429 Preparing to unpack libgssapi-krb5...",
            "#10 5.503 Unpacking libgssapi-krb5...",
            "FAIL: test_check_interrupted (tests.test_process_recovery.ComposeProcessRecovery)",
            "AssertionError: 'masf-check-old' == 'masf-check-old'",
        ])
        self.assertEqual(self.ci_tick(observe_only=True), 0)
        routed = self.ci_job()
        self.assertEqual(routed["state"], "building")
        diagnostic = routed["ci_repair"]["diagnostics"][0]
        self.assertEqual(diagnostic["kind"], "functional")
        self.assertIn("AssertionError", diagnostic["evidence"])
        self.assertNotIn("Preparing to unpack", diagnostic["evidence"])
        self.assertEqual(self.ci_tick(), 0)
        self.assertEqual(self.ci_job()["state"], "pr_open")
        self.assertNotEqual(self.ci_job()["candidate"], self.old_head)
        self.assertEqual(len(self.prs), 1)

    def test_ci_http_status_diagnostics(self):
        from masf.ci import classify

        for category, examples in [
            ("quota", ["HTTP 429", "HTTP/2 429 Too Many Requests", "status code: 429", "Quota exceeded", "API rate limit exceeded"]),
            ("access", ["HTTP 401", "gh: Forbidden (HTTP 403)", "response status: 403", "Bad credentials"]),
            ("infrastructure", ["HTTP/1.1 503", "status=502", "response code: 504", "Connection reset"]),
            ("unknown", ["4.429", "5.503", "org/repo: 403", "test_ci_access_quota_and_configuration ... ok", "test_rate_limit ... ok", "test_connection_reset ... ok"]),
        ]:
            for evidence in examples:
                with self.subTest(evidence=evidence):
                    self.assertEqual(classify({"conclusion": "failure"}, evidence), category)

    def test_ci_access_quota_and_configuration(self):
        from masf import cli

        self.open_ci_pr()
        for attempt, (line, category) in enumerate([
            ("Permission denied", "access"),
            ("Quota exceeded", "quota"),
            ("Invalid workflow", "configuration"),
        ], 1):
            self.action_attempt = attempt
            self.annotation = line
            self.ci_tick()
            job = self.ci_job()
            self.assertEqual(job["state"], "blocked")
            self.assertEqual(job["ci_repair"]["diagnostics"][0]["kind"], category)
            self.assertEqual(job["rounds"], 1)
            self.assertEqual(job.get("ci_technical", 0), 0)
            cli.resume(self.worker.db, self.cfg, self.target)
        self.assertEqual(self.roles.count("build"), 1)
        self.assertEqual(self.worker.remote_head(), self.old_head)

    def test_ci_cancel_checks_eligibility_before_agent(self):
        self.open_ci_pr()
        self.ci_tick(observe_only=True)
        self.issue["labels"] = []
        self.ci_tick()
        self.assertEqual(self.ci_job()["state"], "cancelled")
        self.assertEqual(self.ci_job()["rounds"], 1)
        self.assertEqual(self.worker.remote_head(), self.old_head)

    def test_ci_close_and_scope_change_after_routing(self):
        self.open_ci_pr()
        self.ci_tick(observe_only=True)
        self.issue["body"] = "New scope"
        self.ci_tick()
        self.assertEqual(self.ci_job()["state"], "needs_input")
        self.assertEqual(self.ci_job()["retry"], "planning")
        self.assertEqual(self.worker.remote_head(), self.old_head)
        self.assertEqual(self.roles.count("build"), 1)
        self.prs[0].update(state="closed", merged_at="2026-11-24")
        self.ci_tick()
        self.assertEqual(self.ci_job()["state"], "merged")

    def test_ci_explicit_link_bootstraps_plan(self):
        from masf import cli

        self.open_ci_pr()
        self.worker.db.execute("DELETE FROM jobs")
        self.worker.db.commit()
        self.ci_tick()
        self.assertIsNone(self.worker.db.execute("SELECT data FROM jobs").fetchone())
        cli.link_pr(self.worker.db, self.cfg, self.target, 3, self.old_head, "Scope approved")
        linked = self.ci_job()
        self.assertEqual(linked["decisions"][0]["kind"], "link")
        self.assertTrue(linked["provenance"]["engine"].startswith("sha256:"))
        self.assertEqual(linked["provenance"]["configuration"]["repository"]["base_branch"], "main")
        self.ci_tick()
        self.assertEqual(self.ci_job()["retry"], "planning")
        cli.resume(self.worker.db, self.cfg, self.target)
        self.assertEqual(self.ci_tick(), 0)
        self.assertEqual(self.ci_job()["state"], "pr_open")
        self.assertEqual(self.ci_job()["pr_number"], 3)
        self.assertEqual(self.roles, ["plan", "build", "review", "plan", "build", "review"])


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
