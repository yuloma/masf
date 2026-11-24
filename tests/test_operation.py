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

    def test_publication_uses_rate_limit_headers_and_preserves_json_input(self):
        from masf import queue
        from masf.system import ProcessError

        payload = '{"draft": true}'
        error = ProcessError(
            "gh: HTTP 403", "HTTP/2.0 403 Forbidden\nRetry-After: 120\n\n{}"
        )
        with patch("masf.queue.command", side_effect=error) as invoke:
            with self.assertRaises(ProcessError):
                queue.api("repos/org/repo/pulls", "--input", "-", stdin=payload)
            self.assertEqual(invoke.call_args.kwargs["stdin"], payload)
            self.assertGreater(queue.retry_delay(), 119)


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


class ProviderActivity(unittest.TestCase):
    def test_stream_result_and_incomplete_stream(self):
        result = {
            "version": 1,
            "summary": "Reviewed",
            "verdict": "pass",
            "findings": [],
        }
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "subtype": "success", "result": json.dumps(result)},
        ]
        self.assertEqual(
            parse("cursor", "review", "\n".join(json.dumps(e) for e in events))[0],
            result,
        )
        with self.assertRaisesRegex(ValueError, "incompleto"):
            parse("cursor", "review", "\n".join(json.dumps(e) for e in events[:-1]))

    def test_progress_survives_timeout(self):
        lines = []
        with self.assertRaisesRegex(RuntimeError, "tiempo agotado"):
            command(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time;print('started',flush=True);time.sleep(30)",
                ],
                timeout=0.2,
                on_stdout_line=lines.append,
            )
        self.assertEqual(lines, ["started"])

    def test_spooled_stderr_does_not_block_progress(self):
        lines = []
        output = command(
            [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('x'*200000);print('done')",
            ],
            on_stdout_line=lines.append,
            timeout=3,
        )
        self.assertEqual(output, "done")
        self.assertEqual(lines, ["done"])


class CursorReviewPermissions(unittest.TestCase):
    def test_review_is_noninteractive_with_readonly_code_and_readable_diff(self):
        import tempfile
        from pathlib import Path
        from masf import providers

        diff = "diff --git a/file b/file\n+print(42)\n"
        seen = {}

        def run(args, **kwargs):
            if args[-1] == "--version":
                return "2026.11.22-e9042b2"
            if args[:3] == ["docker", "rm", "-f"]:
                return ""
            seen["args"] = args
            mount = next(value for value in args if "dst=/run/masf-task" in value)
            source = mount.split("src=", 1)[1].split(",dst=", 1)[0]
            seen["prompt"] = Path(source).read_text()
            return json.dumps(
                {
                    "type": "result",
                    "result": json.dumps(
                        {
                            "version": 1,
                            "summary": "Reviewed",
                            "verdict": "pass",
                            "findings": [],
                        }
                    ),
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "code"
            workspace.mkdir()
            cfg = {
                "providers": {"cursor": {"executable": "agent"}},
                "service": {"step_timeout_seconds": 10},
            }
            job = {"repo": {"routing": {"review": "cursor"}, "runner_image": "runner"}}
            with patch("masf.providers.command", side_effect=run):
                providers.invoke("review", job, cfg, workspace, {"diff": diff}, "owner")
            self.assertIn("--force", seen["args"])
            self.assertNotIn("--mode", seen["args"])
            self.assertIn(f"{workspace}:/workspace:ro", seen["args"])
            self.assertIn(diff, seen["prompt"])


class StreamingFailurePrivacy(unittest.TestCase):
    def test_structured_payload_stays_out_of_error_message(self):
        from masf.system import ProcessError

        with self.assertRaises(ProcessError) as caught:
            command(
                [
                    sys.executable,
                    "-c",
                    "import sys;print('private tool payload');sys.exit(2)",
                ],
                on_stdout_line=lambda line: None,
            )
        self.assertNotIn("private tool payload", str(caught.exception))
        self.assertEqual(caught.exception.stdout, "")
