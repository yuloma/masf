import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from masf import cli, config, queue, recovery
from masf.system import CommandTimeout, ProcessError


class RecoveryPolicy(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "service": {"max_recovery_attempts": 2, "recovery_backoff_seconds": 30}
        }
        self.job = {"state": "reviewing", "candidate": "a" * 40}

    def test_backoff_budget_and_rate_limit_survive_serialization(self):
        with patch("masf.recovery.time.time", return_value=1000):
            first = recovery.schedule(self.job, self.cfg, CommandTimeout("timeout"))
            self.assertEqual(first["recovery_wait"]["until"], 1030)
            job = json.loads(json.dumps(self.job | first))
            second = recovery.schedule(
                job, self.cfg, ProcessError("HTTP 429"), minimum_delay=120
            )
            self.assertEqual(second["recovery_wait"]["until"], 1120)
            job.update(json.loads(json.dumps(second)))
            self.assertIsNone(
                recovery.schedule(job, self.cfg, CommandTimeout("timeout"))
            )

    def test_unknown_permanent_and_permission_errors_are_not_retried(self):
        for error in (
            RuntimeError("unknown"),
            ValueError("bad config"),
            ProcessError("HTTP 403 Forbidden"),
            ProcessError("HTTP 401 Unauthorized"),
            RuntimeError("Ruta protegida: .github/ci"),
        ):
            with self.subTest(error=error):
                self.assertIsNone(
                    recovery.schedule(self.job, self.cfg, error, minimum_delay=120)
                )
        for error in (
            CommandTimeout("timeout"),
            recovery.RecoverableFailure("invalid provider output"),
            ProcessError("HTTP 503"),
            ProcessError("Could not resolve host: github.com"),
        ):
            with self.subTest(error=error):
                self.assertIsNotNone(recovery.schedule(self.job, self.cfg, error))

    def test_zero_budget_disables_automatic_recovery(self):
        self.cfg["service"]["max_recovery_attempts"] = 0
        self.assertIsNone(
            recovery.schedule(self.job, self.cfg, CommandTimeout("timeout"))
        )
        with self.assertRaisesRegex(RuntimeError, "agotadas"):
            recovery.reserve(self.job, self.cfg, "resolve_base")

    def test_waiting_job_does_not_reserve_capacity_and_resumes_when_due(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = queue.connect(root)
            self.addCleanup(db.close)
            waiting = {
                "repo_id": 1,
                "number": 1,
                "slug": "org/repo",
                "state": "retry_wait",
                "recovery_wait": {"until": 2000, "stage": "reviewing"},
            }
            queued = waiting | {"number": 2, "state": "queued"}
            for job in (waiting, queued):
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?)",
                    (1, job["number"], job["slug"], job["state"], json.dumps(job)),
                )
            db.commit()
            cfg = {
                "service": {"max_parallel_jobs": 1},
                "repositories": [{"slug": "org/repo", "enabled": True}],
            }
            with (
                patch("masf.cli.time.time", return_value=1000),
                patch("masf.cli.scan", return_value=0),
                patch("masf.cli.process_task", return_value=0) as process,
            ):
                cli.tick(db, cfg, root / "config.toml", root, True)
                self.assertEqual(process.call_args.args[-1]["number"], 2)
            with (
                patch("masf.cli.time.time", return_value=2001),
                patch("masf.cli.scan", return_value=0),
                patch("masf.cli.process_task", return_value=0) as process,
            ):
                cli.tick(db, cfg, root / "config.toml", root, True)
                self.assertEqual(process.call_args.args[-1]["number"], 1)
            with (
                patch("masf.cli.time.time", return_value=1000),
                patch("masf.cli.claim") as claim,
            ):
                self.assertEqual(
                    cli.process_task(cfg, root / "config.toml", root, waiting), 0
                )
                claim.assert_not_called()

    def test_legacy_config_defaults_and_validation(self):
        example = Path(__file__).resolve().parents[1] / "examples/factory.toml"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profiles").mkdir()
            (root / "profiles/postgres.yaml").write_bytes(
                (example.parent / "profiles/postgres.yaml").read_bytes()
            )
            path = root / "config.toml"
            legacy = (
                example.read_text()
                .replace("max_recovery_attempts = 2\n", "")
                .replace("recovery_backoff_seconds = 30\n", "")
            )
            path.write_text(legacy)
            service = config.load(path)["service"]
            self.assertEqual(service["max_recovery_attempts"], 2)
            self.assertEqual(service["recovery_backoff_seconds"], 30)
            for key, value in (
                ("max_recovery_attempts", -1),
                ("max_recovery_attempts", "true"),
                ("recovery_backoff_seconds", 0),
            ):
                path.write_text(
                    legacy.replace("[service]", f"[service]\n{key} = {value}")
                )
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(config.ConfigError, key),
                ):
                    config.load(path)
