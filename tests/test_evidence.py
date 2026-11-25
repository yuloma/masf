import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from masf import cli, providers, queue
from masf.recovery import RecoverableFailure
from masf.system import CommandTimeout, ProcessError
from masf.worker import Worker


class StageEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = queue.connect(self.root)
        self.addCleanup(self.db.close)
        self.repo = {"slug": "org/repo", "enabled": True, "label": "ready",
                     "runner_image": "runner", "routing": {"review": "cursor"},
                     "setup": [], "checks": [["test", "--password", "argv-secret"]]}
        self.cfg = {
            "version": 1, "repositories": [self.repo], "service": {"step_timeout_seconds": 10},
            "providers": {
                "cursor": {"executable": "agent", "model": "auto", "accessToken": "cfg-secret"},
                "codex": {"executable": "codex", "model": "selected-codex"},
            },
        }
        self.issue = {"number": 1, "title": "Test", "body": "password=issue-secret",
                      "state": "open", "labels": [{"name": "ready"}]}

        def lookup(endpoint):
            if "/issues?" in endpoint:
                return [self.issue]
            if "/commits/" in endpoint:
                return {"sha": "base"}
            return {"id": 7, "default_branch": "main"}

        with patch.object(queue, "api", side_effect=lookup):
            self.assertEqual(queue.scan(self.cfg, self.db), 0)
        self.job = json.loads(self.db.execute("SELECT data FROM jobs").fetchone()[0])
        self.worker = Worker(self.db, self.cfg, self.root / "config.toml", self.root,
                             self.root / "jobs", self.job)

    def stages(self, role=None):
        return queue.stage_attempts(self.db, self.job, role=role)

    def run_review(self, mock):
        with patch.object(providers, "command", side_effect=mock):
            return self.worker.role("review", "candidate", self.root, plan={"criteria": ["AC1"]})

    def test_before_version_and_success_with_changed_provider_and_model(self):
        registration = self.job["provenance"]
        self.assertRegex(registration["engine"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(registration["policy"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(registration["configuration"]["providers"]["cursor"]["model"], "auto")

        with self.assertRaisesRegex(ProcessError, "version check"):
            self.run_review(ProcessError("version check failed: api_key=version-secret"))
        before, = self.stages("review")
        self.assertIsNone(before["cli_version"])
        self.assertEqual(before["model"]["availability"], "unknown")
        self.assertEqual(before["status"], "failed")
        self.assertEqual(before["explanation"]["availability"], "available")

        self.job["repo"]["routing"]["review"] = "codex"

        def command(args, *, on_stdout_line=None, **kwargs):
            if args[-1] == "--version":
                return "codex 1"
            if args[2] == "-f":
                return ""
            wire = [
                {"type": "thread.started", "thread_id": "thread-2"},
                {"type": "item.completed", "item": {"type": "agent_message",
                 "text": json.dumps({"version": 1, "summary": "Criterio verificado",
                                     "verdict": "pass", "findings": []})}},
                {"type": "turn.completed", "usage": {"output_tokens": 0}},
            ]
            output = "\n".join(map(json.dumps, wire))
            for line in output.splitlines():
                on_stdout_line(line)
            return output

        response = self.run_review(command)
        first, last = self.stages("review")
        self.assertEqual(first, before)
        self.assertEqual(last["id"], response["stage_attempt"])
        self.assertEqual(last["retry_of"], first["id"])
        self.assertEqual(last["provider"], "codex")
        self.assertEqual(last["execution_id"], "thread-2")
        self.assertEqual(last["cli_version"], "codex 1")
        self.assertEqual(last["model"]["requested"], "selected-codex")
        self.assertIsNone(last["model"]["effective"])
        self.assertEqual(last["usage"]["measurements"][0]["value"], 0)
        self.assertEqual(last["usage"]["measurements"][0]["unit"], "tokens")
        self.assertEqual(last["explanation"]["criteria"], ["AC1"])
        self.assertEqual(last["explanation"]["decisions"], ["pass"])
        self.assertEqual(last["input_sha"], "candidate")
        self.assertGreaterEqual(last["finished_at"], last["started_at"])
        self.assertIsNotNone(last["duration_seconds"])
        self.assertEqual(registration["configuration"]["repository"]["routing"]["review"], "cursor")
        self.assertEqual(last["provenance"]["configuration"]["repository"]["routing"]["review"], "codex")

    def test_active_and_failures_after_version_preserve_reported_metadata(self):
        for error in (CommandTimeout("token=timeout-secret"), ProcessError("process failed"), None):
            with self.subTest(error=error):
                def command(args, *, on_stdout_line=None, **kwargs):
                    if args[-1] == "--version":
                        return "cursor 1 api_key=version-secret"
                    if args[2] == "-f":
                        return ""
                    active = self.stages("review")[-1]
                    self.assertEqual(active["status"], "active")
                    self.assertEqual(active["model"]["availability"], "unknown")
                    self.assertEqual(active["explanation"]["availability"], "unknown")
                    metadata = {"type": "result", "effective_model": "resolved-model",
                                "session_id": "session-1", "cli_version": "cursor 1",
                                "usage": {"inputTokens": 0}, "result": "invalid",
                                "total_cost_usd": 0.25, "billed_cost_usd": 0.10,
                                "metadata": {"api_key": "metadata-secret"}}
                    line = json.dumps(metadata, indent=2)
                    for part in line.splitlines():
                        on_stdout_line(part)
                    self.assertEqual(self.stages("review")[-1]["model"]["availability"], "reported")
                    if error:
                        raise error
                    return line

                with self.assertRaises(type(error) if error else RecoverableFailure):
                    self.run_review(command)
                failure = self.stages("review")[-1]
                self.assertEqual(failure["status"], "failed")
                self.assertIn("cursor 1", failure["cli_version"])
                self.assertEqual(failure["execution_id"], "session-1")
                self.assertEqual(failure["model"]["effective"], "resolved-model")
                self.assertEqual(failure["model"]["source"], "cursor:cli:effective_model")
                self.assertEqual([value.get("kind") for value in failure["usage"]["measurements"]],
                                 [None, "estimated", "confirmed"])
                self.assertIsNotNone(failure["error"])

        data = self.db.execute("SELECT data FROM jobs").fetchone()[0]
        events = "".join(row[0] for row in self.db.execute("SELECT detail FROM events"))
        artifacts = "\n".join(path.read_text() for path in self.worker.evidence.iterdir())
        for value in ("cfg-secret", "argv-secret", "issue-secret", "version-secret",
                      "timeout-secret", "metadata-secret"):
            self.assertNotIn(value, data + events + artifacts)

    def test_check_attempt_filters_commands_and_failure_evidence(self):
        def docker(args, **kwargs):
            if args[:2] == ["docker", "exec"]:
                self.assertIn("argv-secret", args)
                raise ProcessError("docker (1): AssertionError password=check-secret")
            return ""

        with (
            patch.object(self.worker, "checkout", return_value=self.worker.root),
            patch("masf.checks.command", side_effect=docker),
        ):
            report = self.worker.test("candidate")
        stage, = self.stages("checks")
        self.assertFalse(report["passed"])
        self.assertEqual(stage["status"], "failed")
        self.assertEqual(stage["commands"], [["test", "--password", "[redacted]"]])
        self.assertEqual(stage["model"]["availability"], "not_applicable")
        artifact = self.worker.evidence / stage["explanation"]["evidence"][1]
        self.assertEqual(json.loads(artifact.read_text())["sha"], "candidate")
        for value in ("check-secret", "argv-secret"):
            self.assertNotIn(value, artifact.read_text() + json.dumps(stage))

    def test_interrupted_attempt_survives_new_reservation(self):
        self.assertTrue(queue.claim(self.db, self.job, "old-lease", 90))
        self.job["attempt"] = "old-lease"
        with self.assertRaises(KeyboardInterrupt):
            with self.worker.stage("review", "candidate", provider="cursor"):
                raise KeyboardInterrupt()
        original, = self.stages()
        self.assertEqual(original["status"], "active")
        with self.db:
            self.db.execute("DELETE FROM leases")
        self.assertTrue(queue.claim(self.db, self.job, "next-lease", 90))
        self.job["attempt"] = "next-lease"
        queue.interrupt_stages(self.db, self.job)
        with self.worker.stage("review", "candidate", provider="cursor") as retry:
            self.assertEqual(retry["retry_of"], original["id"])
        prior, _ = self.stages()
        self.assertEqual(prior["status"], "interrupted")
        self.assertEqual(prior["lease"], "old-lease")
        self.assertIsNone(prior["duration_seconds"])

    def test_historical_result_adapts_without_guessing_the_model(self):
        historical = self.job | {"review": {"provider": "cursor", "sha": "old-candidate",
                                  "version": "cursor 0", "model": "auto", "usage": {"inputTokens": 15},
                                  "result": {"summary": "Verificado", "verdict": "pass"}}}
        historical.pop("provenance")
        view = cli.job_evidence(self.db, historical, self.root)
        self.assertIsNone(view["provenance"]["engine"])
        entry, = view["stage_attempts"]
        self.assertTrue(entry["historical"])
        self.assertEqual(entry["input_sha"], "old-candidate")
        self.assertEqual(entry["cli_version"], "cursor 0")
        self.assertIsNone(entry["model"]["effective"])
        self.assertEqual(entry["explanation"]["summary"], "Verificado")
        self.assertEqual(entry["usage"]["measurements"],
                         [{"name": "inputTokens", "value": 15, "unit": "tokens",
                           "source": "historical:usage.inputTokens"}])
