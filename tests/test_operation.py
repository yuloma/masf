import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from masf import providers
from masf.providers import parse
from masf.recovery import RecoverableFailure
from masf.system import CommandTimeout, ProcessError, TaskStopped, command


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
        for on_line in (None, lambda line: None):
            calls = 0

            def stop():
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise TaskStopped("cancelled")

            with self.subTest(streaming=on_line is not None), self.assertRaises(TaskStopped):
                command(
                    [sys.executable, "-c", "import time;time.sleep(30)"],
                    guard=stop,
                    on_stdout_line=on_line,
                )

    def test_cancelled_command_never_starts_with_or_without_streaming(self):
        def stop():
            raise TaskStopped("cancelled")

        for on_line in (None, lambda line: None):
            with self.subTest(streaming=on_line is not None), patch(
                "masf.system.subprocess.Popen"
            ) as launch:
                with self.assertRaises(TaskStopped):
                    command([sys.executable, "-c", "pass"], guard=stop, on_stdout_line=on_line)
                launch.assert_not_called()


class ProviderContracts(unittest.TestCase):
    # Synthetic payloads and CLI envelopes for all roles.
    role_fields = {
        "plan": {"criteria": ["AC1 Draft the greeting"]},
        "build": {"changes": ["AC1 Added the greeting"]},
        "review": {"verdict": "pass", "findings": []},
    }
    formats = {
        "codex": ("jsonl",),
        "cursor": ("stream-json", "json"),
        "claude": ("result", "structured_output"),
    }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name) / "code"
        self.workspace.mkdir()

    def envelope(self, output_format, payload):
        if output_format == "jsonl":
            message = {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": payload},
            }
            completed = {"type": "turn.completed", "usage": {"output_tokens": 10}}
            return "\n".join(map(json.dumps, (message, completed)))
        result = {"type": "result", "subtype": "success", "result": payload}
        if output_format == "stream-json":
            return '{"type":"system","subtype":"init"}\n' + json.dumps(result)
        if output_format == "structured_output":
            result = {**result, "result": "Complete", "structured_output": payload}
        return json.dumps(result)

    def run_adapter(self, provider, role, output, *, error=None):
        job = {"repo": {"routing": {role: provider}, "runner_image": "runner"}}
        cfg = {
            "providers": {provider: {"executable": provider}},
            "service": {"step_timeout_seconds": 10},
        }
        activity = []

        def simulate_command(args, *, on_stdout_line=None, **kwargs):
            if args[-1] == "--version":
                return "synthetic version"
            if args[:3] == ["docker", "rm", "-f"]:
                return ""
            if isinstance(output, Exception):
                raise output
            if on_stdout_line:
                for line in output.splitlines():
                    on_stdout_line(line)
            return output

        with patch.object(providers, "command", side_effect=simulate_command) as run:
            args = (role, job, cfg, self.workspace, {}, "test-owner")
            if error is None:
                result = providers.invoke(*args, on_activity=activity.append)
            else:
                with self.assertRaises(error) as caught:
                    providers.invoke(*args, on_activity=activity.append)
                result = caught.exception
        version, execution, cleanup = run.call_args_list
        self.assertEqual(version.args[0][-2:], [provider, "--version"])
        self.assertEqual(activity[0]["event"], "cli_version")
        self.assertEqual(execution.kwargs["timeout"], 10)
        args = execution.args[0]
        name = args[args.index("--name") + 1]
        self.assertEqual(cleanup.args[0], ["docker", "rm", "-f", name])
        return result

    def test_success_and_alternative_role_results_in_each_format(self):
        alternatives = {
            "plan": {"question": "Which greeting should be used?"},
            "review": {
                "verdict": "changes_requested",
                "findings": ["AC1 The greeting is missing."],
            },
        }
        for provider, formats in self.formats.items():
            for output_format in formats:
                for role, fields in self.role_fields.items():
                    cases = [fields]
                    if role in alternatives:
                        cases.append(alternatives[role])
                    for extra in cases:
                        with self.subTest(provider=provider, format=output_format, role=role, fields=extra):
                            expected = {"version": 1, "summary": "Example", **extra}
                            payload = expected if output_format == "structured_output" else json.dumps(expected)
                            actual = self.run_adapter(provider, role, self.envelope(output_format, payload))
                            self.assertEqual(actual["result"], expected)
                            self.assertEqual(actual["provider"], provider)
                            self.assertEqual(actual["version"], "synthetic version")
                            if provider == "codex":
                                self.assertEqual(actual["usage"], {"output_tokens": 10})

    def invalid_cases(self, output_format, role, valid):
        def wrap(value):
            payload = value if output_format == "structured_output" else json.dumps(value)
            return self.envelope(output_format, payload)

        output = wrap(valid)
        yield "malformed envelope", "not JSON"
        yield "non-object envelope", "[]"
        yield "malformed payload", self.envelope(output_format, "not JSON")
        yield "truncated JSON", output[:-1]
        if output_format in ("jsonl", "stream-json"):
            yield "missing terminal event", output.rsplit("\n", 1)[0]
        for field in valid:
            omitted = {key: value for key, value in valid.items() if key != field}
            yield f"missing {field}", wrap(omitted)
            yield f"invalid {field} type", wrap({**valid, field: {"unexpected": True}})
        for version in (True, 1.0):
            yield f"invalid version {version!r}", wrap({**valid, "version": version})
        if role == "plan":
            yield "invalid question type", wrap({
                "version": 1, "summary": "Example", "question": ["Not a string"]
            })
        if role == "review":
            yield "missing requested changes", wrap({**valid, "verdict": "changes_requested"})

    def test_invalid_and_truncated_output_for_every_provider_and_role(self):
        for provider, formats in self.formats.items():
            for output_format in formats:
                for role, extra in self.role_fields.items():
                    valid = {"version": 1, "summary": "Example", **extra}
                    for failure, wire in self.invalid_cases(output_format, role, valid):
                        with self.subTest(provider=provider, format=output_format, role=role, failure=failure):
                            caught = self.run_adapter(provider, role, wire, error=RecoverableFailure)
                            self.assertEqual(str(caught), f"{provider}: salida inválida o incompleta")

    def test_process_failures_and_timeouts_for_every_provider_and_role(self):
        for provider in self.formats:
            for role in self.role_fields:
                for error in (ProcessError("docker (2): process failed"),
                              CommandTimeout("docker: tiempo agotado (10s)")):
                    with self.subTest(provider=provider, role=role, error=type(error).__name__):
                        caught = self.run_adapter(provider, role, error, error=type(error))
                        self.assertIs(caught, error)

    def test_cli_error_envelopes_report_failure(self):
        for provider, event in (
            ("codex", {"type": "turn.failed", "message": "CLI failed"}),
            ("cursor", {"type": "result", "is_error": True, "result": "CLI failed"}),
            ("claude", {"type": "result", "subtype": "error", "result": "CLI failed"}),
        ):
            with self.subTest(provider=provider), self.assertRaisesRegex(RuntimeError, "CLI failed"):
                parse(provider, "plan", json.dumps(event))

    def test_cursor_stream_rejects_nonterminal_and_duplicate_results(self):
        payload = json.dumps({"version": 1, "summary": "Example", **self.role_fields["plan"]})
        output = self.envelope("stream-json", payload)
        progress, result = output.splitlines()
        for invalid in (output + "\n" + progress, output + "\n" + result,
                        output + "\n[]", result.replace('"type": "result"', '"type": "assistant"')):
            with self.subTest(output=invalid), self.assertRaises(ValueError):
                parse("cursor", "plan", invalid)


class GitHubLimits(unittest.TestCase):
    def tearDown(self):
        from masf import queue

        queue._retry_at = 0

    def test_retry_after_is_respected(self):
        from masf import queue
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
                    (1, job["number"], job["slug"], state, json.dumps(job | {"state": state})),
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
                self.assertEqual(cli.tick(db, cfg, root / "config.toml", root, True), 2)
                process.assert_called_once()
            db.close()


class CursorOutput(unittest.TestCase):
    def test_progress_and_final_object_from_real_cli(self):
        fixture = Path(__file__).parent / "fixtures/cursor-2026.11.11-b912975.json"
        output = fixture.read_text(encoding="utf-8")
        capture = json.loads(output)
        self.assertEqual(capture["cli_version"], "2026.11.11-b912975")
        self.assertEqual(capture["session_id"], "fixture")
        self.assertEqual(capture["request_id"], "fixture")
        self.assertTrue(capture["result"].startswith("I'll inspect"))
        result, meta = parse("cursor", "plan", output)
        self.assertEqual(result, {
            "version": 1,
            "summary": "Comprobar en README.md que MASF se presenta como Multi Agent Software Factory.",
            "criteria": ["AC1 README.md declara que MASF corresponde a Multi Agent Software Factory"],
        })
        self.assertEqual(meta["usage"], capture["usage"])

    def test_rejects_truncated_or_nonterminal_object(self):
        for payload in ('Progress {"version":1', 'Progress {"version":1} extra'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse("cursor", "plan", json.dumps({"result": payload}))


class ProviderActivity(unittest.TestCase):
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
