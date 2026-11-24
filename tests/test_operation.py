import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from masf.providers import parse
from masf.system import TaskStopped, command


class Commands(unittest.TestCase):
    def test_timeout_terminates_children(self):
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "tiempo agotado"):
            command([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.1)
        self.assertLess(time.monotonic() - started, 3)

    def test_large_input_survives_slow_reader(self):
        payload = "á" * 200000
        output = command(
            [
                sys.executable,
                "-c",
                "import sys,time;time.sleep(1.2);print(len(sys.stdin.read()))",
            ],
            stdin=payload,
            timeout=5,
        )
        self.assertEqual(output, str(len(payload)))

    def test_guard_terminates_running_process(self):
        def stop():
            raise TaskStopped("cancelled")

        with self.assertRaises(TaskStopped):
            command([sys.executable, "-c", "import time;time.sleep(30)"], guard=stop)

    def test_stream_requires_completion(self):
        payload = json.dumps({"version": 1, "summary": "Plan", "criteria": ["AC1"]})
        message = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": payload},
            }
        )
        with self.assertRaisesRegex(ValueError, "incompleto"):
            parse("codex", "plan", message)
        self.assertEqual(
            parse("codex", "plan", message + '\n{"type":"turn.completed"}')[0][
                "criteria"
            ],
            ["AC1"],
        )

    def test_valid_results_all_roles_and_providers(self):
        for role, extra in [
            ("plan", {"criteria": ["AC1"]}),
            ("build", {"changes": ["AC1"]}),
            ("review", {"verdict": "pass", "findings": []}),
        ]:
            result = {"version": 1, "summary": "ok", **extra}
            for provider in ("codex", "cursor", "claude"):
                if provider == "codex":
                    output = (
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "agent_message",
                                    "text": json.dumps(result),
                                },
                            }
                        )
                        + '\n{"type":"turn.completed"}'
                    )
                else:
                    output = json.dumps({"result": json.dumps(result)})
                with self.subTest(role=role, provider=provider):
                    self.assertEqual(parse(provider, role, output)[0], result)


class GitHubLimits(unittest.TestCase):
    def tearDown(self):
        from masf import queue

        queue._retry_at = 0

    def test_retry_after_is_respected(self):
        from masf import queue
        from masf.system import ProcessError

        error = ProcessError(
            "429", "HTTP/2.0 429 Too Many Requests\nRetry-After: 120\n\n{}", "HTTP 429"
        )
        with patch("masf.queue.command", side_effect=error):
            with self.assertRaises(ProcessError):
                queue.api("repos/test/test")
            self.assertGreater(queue.retry_delay(), 119)
        with patch("masf.queue.command") as invoke:
            with self.assertRaisesRegex(RuntimeError, "límite"):
                queue.api("repos/test/test")
            invoke.assert_not_called()

    def test_success_headers_are_removed(self):
        from masf import queue

        with patch(
            "masf.queue.command",
            return_value='HTTP/2.0 200 OK\nContent-Type: application/json\n\n{"id":42}',
        ):
            self.assertEqual(queue.api("repos/test/test"), {"id": 42})


class Monitoring(unittest.TestCase):
    def test_failed_pr_lookup_does_not_stop_other_jobs(self):
        import tempfile
        from pathlib import Path
        from masf import cli, queue

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = queue.connect(root)
            existing = {"repo_id": 1, "number": 1, "slug": "org/repo", "pr_number": 2}
            queued = {"repo_id": 1, "number": 2, "slug": "org/repo"}
            for job, state in [(existing, "pr_open"), (queued, "queued")]:
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?)",
                    (1, job["number"], job["slug"], state, json.dumps(job)),
                )
            db.commit()
            cfg = {
                "repositories": [{"slug": "org/repo", "enabled": True}],
                "service": {"max_parallel_jobs": 1},
            }
            with (
                patch("masf.cli.scan", return_value=0),
                patch("masf.cli.api", side_effect=RuntimeError("403")),
                patch("masf.cli.process_task", return_value=0) as process,
            ):
                self.assertEqual(cli.tick(db, cfg, root / "config.toml", root, True), 1)
                process.assert_called_once()
            db.close()


class CursorOutput(unittest.TestCase):
    def test_progress_and_final_object_from_real_cli(self):
        from pathlib import Path

        fixture = Path(__file__).parent / "fixtures/cursor-2026.11.11-b912975.json"
        result, meta = parse("cursor", "plan", fixture.read_text())
        self.assertEqual(result["version"], 1)
        self.assertEqual(len(result["criteria"]), 1)
        self.assertIn("usage", meta)

    def test_rejects_truncated_or_nonterminal_object(self):
        for payload in ('Progress {"version":1', 'Progress {"version":1} extra'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse("cursor", "plan", json.dumps({"result": payload}))
