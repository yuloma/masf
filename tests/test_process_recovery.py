"""Acceptance of controller recovery after a real SIGKILL."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from masf.system import command, docker_env

ROOT = Path(__file__).resolve().parents[1]
SLUG = "local/recovery"
REPO_API = f"/repos/{SLUG}"
ISSUE = {
    "number": 1,
    "state": "open",
    "labels": [{"name": "masf:ready"}],
    "title": "Add a greeting",
    "body": "The build must write a greeting.",
}
BRANCH = "refs/heads/masf/issue-1"
MAIN = "refs/heads/main"
IMAGE = "masf-runner:pilot"


class GitHub(HTTPServer):
    def __init__(self, remote):
        self.remote = remote
        self.prs = []
        self.posts = []
        super().__init__(("127.0.0.1", 0), GitHubHandler)

    def ref(self, ref):
        return command(["git", "--git-dir", str(self.remote), "rev-parse", ref], timeout=5)


class GitHubHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_args):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        routes = {
            REPO_API: lambda: {"id": 42, "default_branch": "main"},
            REPO_API + "/issues": lambda: [ISSUE],
            REPO_API + "/issues/1": lambda: ISSUE,
            REPO_API + "/commits/main": lambda: {"sha": self.server.ref(MAIN)},
        }
        if url.path in routes:
            self.reply(200, routes[url.path]())
        elif (
            url.path == REPO_API + "/pulls"
            and parse_qs(url.query)
            == {"head": ["local:masf/issue-1"], "base": ["main"], "state": ["all"]}
        ):
            self.reply(200, self.server.prs)
        else:
            self.reply(404, {"path": self.path})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.posts.append(payload)
        if (
            self.path != REPO_API + "/pulls"
            or payload.get("head") != "masf/issue-1"
            or payload.get("base") != "main"
            or self.server.prs
        ):
            self.reply(422, {"path": self.path, "payload": payload})
            return
        pr = {
            "number": 17,
            "state": "open",
            "html_url": f"http://127.0.0.1:{self.server.server_port}{REPO_API}/pulls/17",
            "head": {"sha": self.server.ref(BRANCH), "repo": {"full_name": SLUG}},
            "base": {"ref": payload["base"]},
            "body": payload["body"],
        }
        self.server.prs.append(pr)
        self.reply(201, pr)


class RecoveryHarness(unittest.TestCase):
    use_engine = False
    deadline_seconds = 30

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("MASF_JOBS_DIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "source"
        self.remote = self.root / "remote.git"
        self.config = self.root / "factory.toml"
        self.database = self.root / "state/masf/queue.sqlite3"
        self.evidence = self.database.parent / "evidence/42-1"
        self.outputs = []

        self.git("init", "--bare", str(self.remote))
        self.source.mkdir()
        self.git("init", "-b", "main")
        (self.source / "check.sh").write_text(
            "set -eu\n"
            "if [ -f greeting ]; then\n"
            "    test \"$(cat greeting)\" = Hello\n"
            "    if [ \"${1:-}\" = block ]; then\n"
            "        touch check-ready\n"
            "        timeout 90 sh -c 'until [ -f check-release ]; do sleep 0.1; done'\n"
            "    fi\n"
            "fi\n"
        )
        self.base = self.commit_base("initial base")
        self.git("push", str(self.remote), "HEAD:" + MAIN)
        check = ["sh", "check.sh", "block"] if self.use_engine else ["sh", "check.sh"]
        self.config.write_text(
            f'''version = 1

[service]
poll_seconds = 1
max_parallel_jobs = 1
max_build_rounds = 2
step_timeout_seconds = 90

[runner]
image = "{IMAGE}"

[providers.codex]
enabled = true
executable = "unused"

[routing]
plan = "codex"
build = "codex"
review = "codex"

[[repositories]]
slug = "{SLUG}"
enabled = true
label = "masf:ready"
setup = []
checks = {json.dumps([check])}
'''
            + (
                '\n[repositories.compose]\nfiles = ["profile.yaml"]\nservice = "app"\n'
                if self.use_engine else ""
            )
        )
        self.github = GitHub(self.remote)
        self.addCleanup(self.github.server_close)
        thread = threading.Thread(
            target=self.github.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 6)
        self.addCleanup(self.github.shutdown)

    def git(self, *args):
        return command(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                "-c", "core.hooksPath=/dev/null", *args,
            ],
            cwd=self.source if self.source.exists() else self.root,
            env=os.environ | {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ALLOW_PROTOCOL": "file",
            },
            timeout=10,
        )

    def commit_base(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def advance_base(self):
        (self.source / "upstream").write_text("Changed on main\n")
        advanced = self.commit_base("advance base")
        self.git("push", str(self.remote), "HEAD:" + MAIN)
        self.assertEqual(self.github.ref(MAIN), advanced)
        return advanced

    def start(self, point=""):
        env = docker_env() | {
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "XDG_STATE_HOME": str(self.database.parent.parent),
            "MASF_JOBS_DIR": str(self.root / "jobs"),
            "CRASH_ROOT": str(self.root),
            "CRASH_POINT": point,
            "CRASH_GITHUB": f"http://127.0.0.1:{self.github.server_port}",
            "CRASH_ENGINE": str(int(self.use_engine)),
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.remote.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": f"https://github.com/{SLUG}.git",
        }
        output = self.root / f"controller-{len(self.outputs)}.log"
        with output.open("w") as log:
            child = subprocess.Popen(
                [
                    sys.executable, str(ROOT / "tests/fixtures/crash_controller.py"),
                    "--config", str(self.config), "run", "--once",
                ],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
            )
        self.outputs.append(output)
        self.addCleanup(self.reap, child)
        return child

    def reap(self, child):
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail(self.diagnostic(f"collect controller {child.pid}"))

    def sqlite(self):
        return sqlite3.connect(f"{self.database.as_uri()}?mode=rw", uri=True, timeout=2)

    def snapshot(self):
        with closing(self.sqlite()) as db:
            jobs = db.execute("SELECT rowid, repo_id, number, slug, state, data FROM jobs").fetchall()
            leases = db.execute("SELECT * FROM leases").fetchall()
            events = db.execute("SELECT * FROM events ORDER BY id").fetchall()
        return jobs, leases, events

    def job(self):
        jobs, _, _ = self.snapshot()
        self.assertEqual(len(jobs), 1, jobs)
        return json.loads(jobs[0][-1])

    def calls(self):
        return [tuple(call) for call in self.trace("calls.jsonl")]

    def trace(self, name):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def diagnostic(self, expected):
        try:
            state = self.snapshot()
        except sqlite3.Error as exc:
            state = str(exc)
        output = "\n".join(f"{path.name}:\n{path.read_text()}" for path in self.outputs)
        return (
            f"Expected {expected}\nSQLite: {state!r}\n"
            f"Calls: {self.calls()!r}\nGitHub POSTs: {self.github.posts!r}\n{output}"
        )

    def wait_for(self, child, expected, predicate):
        deadline = time.monotonic() + self.deadline_seconds
        while time.monotonic() < deadline:
            if child.poll() is not None:
                break
            result = predicate()
            if result:
                return result
            time.sleep(0.025)
        self.fail(self.diagnostic(f"{expected} (exit={child.poll()})"))

    def assert_exit(self, child, code, expected):
        try:
            actual = child.wait(timeout=self.deadline_seconds)
        except subprocess.TimeoutExpired:
            self.fail(self.diagnostic(expected))
        if actual != code:
            self.fail(self.diagnostic(f"{expected} (exit={actual}, wanted {code})"))

    def kill(self, child):
        self.assertIsNone(child.poll())
        child.kill()
        self.assert_exit(child, -signal.SIGKILL, "controller terminated by SIGKILL")

    def expire(self, token):
        with closing(self.sqlite()) as db, db:
            result = db.execute(
                "UPDATE leases SET expires=unixepoch()-1 WHERE token=? AND expires>unixepoch()",
                (token,),
            )
            self.assertEqual(result.rowcount, 1)

    def recover(self, previous):
        _, leases, _ = previous
        self.assertEqual(len(leases), 1, previous)
        repo_id, number, token, _ = leases[0]
        self.assertEqual((repo_id, number), (42, 1))
        calls, posts = self.calls(), list(self.github.posts)
        self.assert_exit(self.start(), 0, "controller skips the unexpired reservation")
        self.assertEqual(self.snapshot(), previous)
        self.assertEqual(self.calls(), calls)
        self.assertEqual(self.github.posts, posts)
        self.expire(token)
        return self.start()

    def assert_delivered(self, previous, sha=None):
        before, leases, old_events = previous
        jobs, live, events = self.snapshot()
        self.assertEqual(len(jobs), 1, jobs)
        self.assertEqual(jobs[0][:4], before[0][:4])
        self.assertEqual(jobs[0][4], "pr_open")
        job = json.loads(jobs[0][-1])
        original = json.loads(before[0][-1])
        if "owner" in original:
            self.assertEqual(job["owner"], original["owner"])
        self.assertNotEqual(job["attempt"], leases[0][2])
        self.assertEqual(events[:len(old_events)], old_events)
        self.assertEqual({event[4] for event in events[len(old_events):]}, {job["attempt"]})
        self.assertEqual(live, [])
        self.assertEqual(job["rounds"], 1)

        self.assertEqual(len(self.github.posts), 1)
        self.assertEqual(len(self.github.prs), 1)
        pr = self.github.prs[0]
        self.assertEqual(job["pr_number"], pr["number"])
        self.assertEqual(job["pr"], pr["html_url"])
        candidate = job["candidate"]
        if sha is not None:
            self.assertEqual(candidate, sha)
        self.assertEqual(candidate, job["intent"])
        self.assertEqual(candidate, self.github.ref(BRANCH))
        self.assertEqual(candidate, pr["head"]["sha"])
        self.assertEqual(job["checks"]["sha"], candidate)
        self.assertTrue(job["checks"]["passed"])
        self.assertTrue(job["checks"]["commands"])
        self.assertTrue(all(entry["code"] == 0 for entry in job["checks"]["commands"]))
        self.assertEqual(job["review"]["sha"], candidate)
        self.assertEqual(job["review"]["result"]["verdict"], "pass")
        self.assertIn(f"<!-- masf:{job['owner']} -->", pr["body"])
        self.assertIn(f"Checks sobre {candidate}", pr["body"])
        self.assertIn("Greeting reviewed", pr["body"])
        return job

    def initial_calls(self, candidate):
        return [
            ("check", self.base), ("plan", self.base), ("build", self.base),
            ("check", candidate), ("review", candidate),
        ]


class ProcessRecovery(RecoveryHarness):
    def test_reserved(self):
        child = self.start("reserved")
        self.wait_for(child, "committed reservation", (self.root / "reserved").exists)
        previous = self.snapshot()
        self.assertEqual(self.job()["state"], "queued")
        self.assertEqual(previous[2], [])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.github.posts, [])
        self.kill(child)

        self.assert_exit(self.recover(previous), 0, "delivery after reservation recovery")
        job = self.assert_delivered(previous)
        self.assertEqual(self.calls(), self.initial_calls(job["candidate"]))

    def publication(self, point, advance=False):
        child = self.start(point)
        self.wait_for(child, "publication checkpoint " + point, (self.root / point).exists)
        previous = self.snapshot()
        pending = self.job()
        sha = pending["candidate"]
        self.assertEqual(pending["state"], "publishing")
        self.assertNotIn("pr_number", pending)
        self.assertEqual(pending["intent"], sha)
        self.assertEqual(self.github.ref(BRANCH), sha)
        self.assertEqual(self.calls(), self.initial_calls(sha))
        if point == "created":
            self.assertEqual(len(self.github.posts), 1)
            self.assertEqual(len(self.github.prs), 1)
            self.assertEqual(self.github.prs[0]["head"]["sha"], sha)
        else:
            self.assertEqual(self.github.posts, [])
            self.assertEqual(self.github.prs, [])
        self.kill(child)

        current = self.advance_base() if advance else self.base
        self.assert_exit(self.recover(previous), 0, f"delivery after {point}")
        if advance and point == "pushed":
            job = self.assert_delivered(previous)
            revised = job["candidate"]
            self.assertNotEqual(revised, sha)
            self.assertEqual(job["base_checked"], current)
            self.assertEqual(self.github.ref(BRANCH + "^"), current)
            self.assertEqual(self.calls(), self.initial_calls(sha) + [
                ("check", current), ("check", revised), ("review", revised),
            ])
        else:
            job = self.assert_delivered(previous, sha)
            self.assertEqual(job["checks"], pending["checks"])
            self.assertEqual(job["review"], pending["review"])
            self.assertEqual(self.calls(), self.initial_calls(sha))
        self.assertEqual(self.github.ref(MAIN), current)

    def test_pushed(self):
        self.publication("pushed")

    def test_created(self):
        self.publication("created")

    def test_pushed_then_base_advanced(self):
        self.publication("pushed", advance=True)

    def test_created_then_base_advanced(self):
        self.publication("created", advance=True)


@unittest.skipUnless(os.environ.get("MASF_TEST_ENGINE") == "1", "requires local Docker Engine")
class ComposeProcessRecovery(RecoveryHarness):
    use_engine = True
    deadline_seconds = 45

    def setUp(self):
        super().setUp()
        self.docker("image", "inspect", IMAGE)
        self.control = "masf-recovery-control-" + uuid.uuid4().hex
        self.control_file = self.root / "control.yaml"
        self.profile = self.root / "profile.yaml"
        self.profile.write_text(self.manifest("original", checkout=True))
        self.control_file.write_text(self.manifest("original", checkout=False))
        self.addCleanup(self.control_command, "down", "--volumes", "--remove-orphans")
        self.addCleanup(self.cleanup_checks)
        self.control_command("up", "-d", "--wait", "--wait-timeout", "20")
        self.control_command("exec", "-T", "app", "sh", "-c", "echo control-data >/data/probe")
        self.control_resources = self.resources(self.control)
        self.assert_project(self.control_resources, self.control + "_original")

    @staticmethod
    def manifest(volume, *, checkout):
        app = {
            "image": IMAGE,
            "command": ["sleep", "infinity"],
            "working_dir": "/workspace" if checkout else "/",
            "volumes": [f"{volume}:/data"],
        }
        if checkout:
            app["user"] = f"{os.getuid()}:{os.getgid()}"
            app["volumes"].append(
                {"type": "bind", "source": "${MASF_WORKSPACE}", "target": "/workspace"}
            )
        return json.dumps({
            "services": {"app": app},
            "volumes": {volume: {}},
            "networks": {"default": {"internal": True}},
        })

    def docker(self, *args, env=None):
        return command(["docker", *args], env=env or docker_env(), timeout=25)

    def control_command(self, *args):
        return self.docker(
            "compose", "--env-file", os.devnull, "-p", self.control,
            "-f", str(self.control_file), *args,
        )

    def cleanup_checks(self):
        errors = []
        for journal in self.evidence.glob("*/cleanup.json"):
            argv, workspace = json.loads(journal.read_text())
            try:
                self.docker(*argv[1:], env=docker_env() | {"MASF_WORKSPACE": workspace})
            except RuntimeError as exc:
                errors.append(f"{journal}: {exc}")
            else:
                journal.unlink()
        if errors:
            self.fail(self.diagnostic("Compose fixture cleanup") + "\n" + "\n".join(errors))

    def resources(self, project):
        label = "label=com.docker.compose.project=" + project
        return {
            kind: self.docker(kind, "ls", *flags, "--filter", label).splitlines()
            for kind, flags in (("container", ("-aq",)), ("network", ("-q",)), ("volume", ("-q",)))
        }

    def assert_project(self, resources, volume):
        self.assertEqual(len(resources["container"]), 1, resources)
        self.assertEqual(len(resources["network"]), 1, resources)
        self.assertEqual(resources["volume"], [volume])

    def assert_control(self):
        self.assertEqual(self.resources(self.control), self.control_resources)
        self.assertEqual(
            self.control_command("exec", "-T", "app", "cat", "/data/probe"),
            "control-data",
        )

    def active_check(self, child, expected, *, previous_journal=None):
        def ready():
            for journal in self.evidence.glob("*/cleanup.json"):
                # A killed controller leaves both the journal and the ready marker.
                if journal == previous_journal:
                    continue
                try:
                    argv, workspace = json.loads(journal.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    # The journal may be getting written or removed by a base check.
                    continue
                workspace = Path(workspace)
                if (workspace / "check-ready").exists():
                    report = json.loads(journal.with_name("checks.json").read_text())
                    self.assertEqual(argv[argv.index("-p") + 1], report["name"])
                    profile = journal.with_name("profile-0.yaml")
                    self.assertEqual(argv[argv.index("-f") + 1], str(profile))
                    self.assertEqual(argv[-3:], ["down", "--volumes", "--remove-orphans"])
                    return report, journal, profile, workspace

        return self.wait_for(child, expected, ready)

    def assert_job_projects(self, active=None):
        reported = set()
        for path in self.evidence.glob("*/checks.json"):
            name = json.loads(path.read_text())["name"]
            reported.add(name)
            if name != active:
                self.assertFalse(any(self.resources(name).values()), name)
        if active:
            self.assertIn(active, reported)

    def test_check_interrupted(self):
        child = self.start()
        report, old_journal, frozen, _ = self.active_check(child, "first candidate check")
        self.assertEqual(list(self.evidence.glob("*/cleanup.json")), [old_journal])
        old = report["name"]
        frozen_cleanup = json.loads(old_journal.read_text())
        original_manifest = self.profile.read_text()
        self.assertEqual(frozen.read_text(), original_manifest)
        old_resources = self.resources(old)
        self.assert_project(old_resources, old + "_original")
        self.assert_control()
        previous = self.snapshot()
        pending = self.job()
        candidate = pending["candidate"]
        self.assertEqual(report["sha"], candidate)
        partial_calls = self.initial_calls(candidate)[:-1]
        self.assertEqual(pending["state"], "checking")
        self.assertEqual(self.calls(), partial_calls)
        self.assertEqual(self.github.posts, [])
        self.kill(child)
        self.assertEqual(self.resources(old), old_resources)
        self.assertTrue(old_journal.exists())

        replacement_manifest = self.manifest("replacement", checkout=True)
        self.profile.write_text(replacement_manifest)
        child = self.recover(previous)
        report, journal, profile, workspace = self.active_check(
            child, "replacement candidate check", previous_journal=old_journal
        )
        active = report["name"]
        self.assertEqual(report["sha"], candidate)
        self.assertNotEqual(active, old)
        self.assertEqual(frozen.read_text(), original_manifest)
        self.assertEqual(profile.read_text(), replacement_manifest)
        self.assertFalse(old_journal.exists())
        self.assertEqual(self.trace("cleanup.jsonl"), [frozen_cleanup])
        self.assertEqual(list(self.evidence.glob("*/cleanup.json")), [journal])
        self.assert_project(self.resources(active), active + "_replacement")
        self.assert_job_projects(active)
        self.assert_control()

        (workspace / "check-release").touch()
        self.assert_exit(child, 0, "delivery after Compose check recovery")
        job = self.assert_delivered(previous, candidate)
        self.assertEqual(job["plan"], pending["plan"])
        self.assertEqual(job["build"], pending["build"])
        self.assertEqual(job["checks"]["name"], active)
        self.assertEqual(self.calls(), partial_calls + [("check", candidate), ("review", candidate)])
        self.assertEqual(list(self.evidence.glob("*/cleanup.json")), [])
        self.assert_job_projects()
        self.assert_control()
