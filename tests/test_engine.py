"""Integration checks on a local Docker Engine, enabled explicitly by the operator."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from masf.checks import compose_files, verify
from masf.config import load
from masf.system import command, docker_env

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("MASF_TEST_ENGINE") == "1", "requires local Docker Engine"
)
class ComposeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("MASF_JOBS_DIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = load(ROOT / "examples/factory.toml")["repositories"][1]
        self.profile = self.root / "postgres.yaml"
        self.profile.write_bytes(
            (ROOT / "examples/profiles/postgres.yaml").read_bytes()
        )
        self.repo["compose"]["files"] = ["postgres.yaml"]
        self.config = self.root / "config.toml"

    def run_profile(self, name, subnet):
        import copy

        repo = copy.deepcopy(self.repo)
        folder = self.root / name
        folder.mkdir(mode=0o755)
        (folder / "schema.sql").write_text("CREATE TABLE probe(id int);")
        (folder / "tests").mkdir()
        (folder / "tests/smoke.sql").write_text("SELECT 1 FROM probe;")
        if subnet:
            override = name + ".yaml"
            (self.root / override).write_text(
                json.dumps(
                    {
                        "services": {},
                        "networks": {
                            "default": {"ipam": {"config": [{"subnet": subnet}]}}
                        },
                    }
                )
            )
            repo["compose"]["files"].append(override)
        return verify(
            repo,
            self.config,
            folder,
            name,
            self.root / ("evidence-" + name),
            "test",
            60,
        )

    def test_concurrent(self):
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(
                self.run_profile, "base", os.getenv("MASF_SMOKE_BASE_SUBNET")
            )
            b = pool.submit(
                self.run_profile, "candidate", os.getenv("MASF_SMOKE_CANDIDATE_SUBNET")
            )
            records = [a.result(), b.result()]
        self.assertTrue(all(r["passed"] for r in records))
        self.assertNotEqual(records[0]["name"], records[1]["name"])
        for record in records:
            for resource in ("network", "volume"):
                output = command(
                    [
                        "docker",
                        resource,
                        "ls",
                        "-q",
                        "--filter",
                        "label=com.docker.compose.project=" + record["name"],
                    ],
                    env=docker_env(),
                )
                self.assertFalse(output)

    def test_nonzero(self):
        self.repo["checks"] = [["sh", "-c", "exit 7"]]
        record = self.run_profile("failure", os.getenv("MASF_SMOKE_BASE_SUBNET"))
        self.assertFalse(record["passed"])
        self.assertEqual(record["commands"][-1]["code"], 7)

    def test_socket_rejected_before_up(self):
        self.profile.write_text(
            self.profile.read_text().replace(
                "${MASF_WORKSPACE:?Falta el checkout de la prueba}",
                "/var/run/docker.sock",
            )
        )
        with self.assertRaisesRegex(ValueError, "Montaje"):
            compose_files(self.config, self.repo["compose"], self.root, self.root)

    def test_unhealthy_service(self):
        self.profile.write_text(
            self.profile.read_text()
            .replace(
                '["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "pilot", "-d", "pilot"]',
                '["CMD", "false"]',
            )
            .replace("retries: 30", "retries: 1")
        )
        with self.assertRaisesRegex(RuntimeError, "unhealthy"):
            self.run_profile("unhealthy", os.getenv("MASF_SMOKE_BASE_SUBNET"))
        self.assertFalse((self.root / "evidence-unhealthy/cleanup.json").exists())

    def test_recover_interrupted_cleanup(self):
        from unittest.mock import patch
        from masf.worker import Worker

        def interrupt(args, **kwargs):
            if "down" in args:
                raise RuntimeError("controller stopped")
            return command(args, **kwargs)

        with (
            patch("masf.checks.command", interrupt),
            self.assertRaisesRegex(RuntimeError, "controller stopped"),
        ):
            self.run_profile("interrupted", os.getenv("MASF_SMOKE_BASE_SUBNET"))
        evidence = self.root / "evidence-interrupted"
        name = json.loads((evidence / "checks.json").read_text())["name"]
        volume = [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + name,
        ]
        self.assertTrue(command(volume))
        job = {"repo_id": 1, "number": 1, "slug": "test/test"}
        worker = Worker(None, {}, self.config, self.root, self.root / "jobs", job)
        worker.evidence = self.root
        worker.recover()
        self.assertFalse(command(volume))
        self.assertFalse((evidence / "cleanup.json").exists())

    def test_cancel_running_check_removes_container(self):
        import time
        from masf.system import TaskStopped

        workspace = self.root / "cancel-workspace"
        workspace.mkdir()
        repo = {
            "runner_image": "masf-runner:pilot",
            "setup": [],
            "checks": [["sleep", "30"]],
        }
        folder = self.root / "cancel-evidence"
        started = time.monotonic()

        def guard():
            if time.monotonic() - started > 0.7:
                raise TaskStopped("cancelled")

        with self.assertRaises(TaskStopped):
            verify(
                repo,
                self.config,
                workspace,
                "cancelled",
                folder,
                "cancel-test",
                30,
                guard=guard,
            )
        report = json.loads((folder / "checks.json").read_text())
        self.assertFalse((folder / "cleanup.json").exists())
        self.assertFalse(
            command(
                ["docker", "ps", "-aq", "--filter", "name=^/" + report["name"] + "$"]
            )
        )

    def test_total_check_deadline(self):
        import time

        workspace = self.root / "deadline-workspace"
        workspace.mkdir()
        repo = {
            "runner_image": "masf-runner:pilot",
            "setup": [["sleep", "0.6"]],
            "checks": [["sleep", "0.6"]],
        }
        started = time.monotonic()
        report = verify(
            repo,
            self.config,
            workspace,
            "deadline",
            self.root / "deadline-evidence",
            "deadline-test",
            1,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["failure_kind"], "infrastructure")
        self.assertLess(time.monotonic() - started, 3)
