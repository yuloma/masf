import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from masf import config, providers, queue, worker
from masf.system import command

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples/factory.toml"


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
    def test_two_pages_two_repositories_concurrent_scans(self):
        self.cfg["repositories"][1]["enabled"] = True

        def api(endpoint):
            if "issues?" in endpoint:
                if endpoint.endswith("page=1"):
                    return [self.issue, self.issue | {"pull_request": {}}] + [
                        self.issue | {"labels": []}
                    ] * 98
                return [self.issue | {"number": 2}]
            if "/commits/" in endpoint:
                return {"sha": "a" * 40}
            return dict(id=2 if endpoint.endswith("sql") else 1, default_branch="main")

        def scan(_):
            db = queue.connect(self.root)
            try:
                return queue.scan(self.cfg, db)
            finally:
                db.close()

        with patch.object(queue, "api", api), ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(scan, range(2))), [0, 0])
        self.assertEqual(
            queue.connect(self.root).execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            4,
        )

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
                    providers.parse(name, "review", json.dumps(example))[0], result
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

    def agent(self, role, job, cfg, workspace, inputs, owner):
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
