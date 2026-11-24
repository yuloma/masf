"""Integration checks on a local Docker Engine, enabled explicitly by the operator."""

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import yaml

from masf.checks import compose_files, verify
from masf.config import load
from masf.system import TaskStopped, command, docker_env
from masf.worker import Worker

ROOT = Path(__file__).resolve().parents[1]
POSTGRES = "postgres:17-alpine"
PSQL = ["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c"]
ENGINE_ENABLED = os.environ.get("MASF_TEST_ENGINE") == "1"


@unittest.skipUnless(ENGINE_ENABLED, "requires local Docker Engine")
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


@unittest.skipUnless(ENGINE_ENABLED, "requires local Docker Engine")
class PrivateComposeChecks(unittest.TestCase):
    def scratch(self, parent):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get(parent))
        self.addCleanup(temp.cleanup)
        return Path(temp.name).resolve()

    def setUp(self):
        self.jobs = self.scratch("MASF_JOBS_DIR")
        self.state = self.scratch("MASF_TEST_ENGINE_STATE_DIR")
        self.config = self.scratch("MASF_TEST_ENGINE_CONFIG_DIR") / "factory.toml"
        self.source = self.jobs / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.shas = {}
        for revision in ("base", "candidate"):
            self.commit(revision)
            self.shas[revision] = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return command(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args,
            ],
            cwd=self.source,
        )

    def commit(self, revision):
        (self.source / "revision.txt").write_text(revision + "\n")
        (self.source / "Dockerfile").write_text(
            f"FROM {POSTGRES}\nCOPY revision.txt /workspace/revision.txt\n"
        )
        (self.source / "alternate").mkdir(exist_ok=True)
        (self.source / "alternate/revision.txt").write_text("alternate\n")
        (self.source / "alternate/Dockerfile").write_text(
            f"FROM {POSTGRES}\nCOPY revision.txt /workspace/revision.txt\n"
        )
        image = f"127.0.0.1:1/masf-alternate:{revision}"
        (self.source / ".env").write_text(
            f"COMPOSE_PROJECT_NAME=masf-alternate-{revision}\n"
            "COMPOSE_FILE=compose.yaml:compose.override.yaml\n"
            "MASF_WORKSPACE=./alternate\n"
            f"MASF_TEST_PG_IMAGE={image}\n"
        )
        (self.source / "compose.yaml").write_text(json.dumps({
            "services": {
                "app": {
                    "image": image,
                    "volumes": ["./alternate:/workspace:ro", "alternate:/alternate"],
                },
            },
            "volumes": {"alternate": {}},
            "networks": {"default": {"internal": False}},
        }))
        (self.source / "compose.override.yaml").write_text(json.dumps({
            "services": {"db": {"image": image, "volumes": ["override:/override"]}},
            "volumes": {"override": {}},
        }))
        self.git("add", "--force", ".")
        self.git("commit", "-qm", revision)

    def docker(self, *args, **kwargs):
        return command(["docker", *args], env=docker_env(), **kwargs)

    def inventory(self, project, image=None):
        label = "label=com.docker.compose.project=" + project
        resources = {
            kind: sorted(self.docker(kind, "ls", *flags, "--filter", label).splitlines())
            for kind, flags in (
                ("container", ["-aq", "--no-trunc"]),
                ("network", ["-q", "--no-trunc"]),
                ("volume", ["-q"]),
                ("image", ["-aq", "--no-trunc"]),
            )
        }
        if image:
            resources["project_image"] = self.docker(
                "image", "ls", "-q", "--no-trunc", "--filter", f"reference={image}:latest"
            ).splitlines()
        return resources

    def inspect(self, *ids):
        return json.loads(self.docker("inspect", *ids))

    def start_control(self, profile):
        project = "masf-control-test-" + uuid.uuid4().hex
        env = docker_env() | {"MASF_WORKSPACE": str(self.source)}
        stack = [
            "docker", "compose", "--env-file", "/dev/null", "--project-directory",
            str(profile.parent), "-p", project, "-f", str(profile),
        ]
        self.addCleanup(command, stack + ["down", "--volumes"], env=env)
        command(stack + ["up", "-d", "--wait", "--wait-timeout", "60"], env=env)

        def sql(query):
            return command(stack + ["exec", "-T", "app", *PSQL, query], env=env)

        def snapshot():
            resources = self.inventory(project)
            containers = self.inspect(*resources["container"])
            self.assertEqual(
                {item["Config"]["Labels"]["com.docker.compose.service"] for item in containers},
                {"app", "db"},
            )
            self.assertTrue(all(item["State"]["Running"] for item in containers))
            self.assertEqual(sql("SELECT id FROM control"), "42")
            return resources

        sql("CREATE TABLE control(id int); INSERT INTO control VALUES (42)")
        return project, snapshot

    def exercise_profiles(self, build):
        private = self.config.parent
        pilot = ROOT / "examples/profiles/postgres.yaml"
        control_profile = private / "control.yaml"
        control_profile.write_bytes(pilot.read_bytes())
        profile = yaml.safe_load(pilot.read_text())
        app = profile["services"]["app"]
        profile["services"]["db"]["image"] = "${MASF_TEST_PG_IMAGE:-" + POSTGRES + "}"
        if build:
            app.pop("image")
            app.pop("volumes")
            app["build"] = {"context": "${MASF_WORKSPACE}"}
        else:
            app["image"] = profile["services"]["db"]["image"]
        # Give the image's declared data path a project volume in both services.
        app.setdefault("volumes", []).append("app-data:/var/lib/postgresql/data")
        profile["volumes"]["app-data"] = {}
        (private / "approved.yaml").write_text(json.dumps(profile))
        self.config.write_text((ROOT / "examples/factory.toml").read_text().replace(
            "profiles/postgres.yaml", "approved.yaml"
        ))
        cfg = load(self.config)
        repo = cfg["repositories"][1]
        repo["setup"] = [PSQL + ["CREATE TABLE probe(id int)"]]
        shared_image = self.inspect(POSTGRES)[0]["Id"]
        control, control_snapshot = self.start_control(control_profile)
        expected_control = control_snapshot()
        worker = Worker(None, cfg, self.config, self.state, self.jobs, {
            "repo_id": 1, "number": 1, "slug": repo["slug"], "repo": repo,
        })
        command(["git", "clone", "-q", "--bare", str(self.source), str(worker.store)])
        self.addCleanup(worker.recover)

        attempts = []
        for revision, outcome in (
            ("base", "passed"), ("candidate", "failed"), ("candidate", "passed"),
            ("candidate", "cancelled"), ("candidate", "interrupted"),
        ):
            with self.subTest(revision=revision, outcome=outcome):
                attempts.append(self.run_attempt(worker, revision, outcome, build, shared_image))
                current_control = control_snapshot()
                self.assertEqual(current_control, expected_control, control)
                self.assertEqual(self.inspect(POSTGRES)[0]["Id"], shared_image)
                print(json.dumps({
                    "control_project": control, "after_attempt": outcome,
                    "sha": self.shas[revision], "row": 42, "resources": current_control,
                    "shared_image": shared_image,
                }, sort_keys=True), flush=True)
        self.assertEqual(len(attempts), 5)
        for field in ("name", "workspace"):
            self.assertEqual(len({attempt[field] for attempt in attempts}), len(attempts), field)
        volumes = {volume for attempt in attempts for volume in attempt["volumes"]}
        self.assertEqual(len(volumes), 2 * len(attempts))

    def run_attempt(self, worker, revision, outcome, build, shared_image):
        sha = self.shas[revision]
        self.git("checkout", "-q", "--detach", sha)
        evidence = {}
        checks = [
            ["cat", "revision.txt"],
            PSQL + ["SELECT count(*) FROM probe"],
            PSQL + ["INSERT INTO probe VALUES (1)"],
            PSQL + ["SELECT id FROM probe"],
        ]
        cancel_check = ["sh", "-c", "touch /tmp/masf-check-started && exec sleep 30"]
        if outcome == "failed":
            checks.append(["sh", "-c", "exit 7"])
        if outcome == "cancelled":
            checks.append(cancel_check)
        worker.job["repo"]["checks"] = checks

        def guard():
            if evidence.get("check_running") and self.docker(
                "exec", evidence["app_id"], "sh", "-c",
                "if test -f /tmp/masf-check-started; then echo started; fi",
            ) == "started":
                evidence["cancelled_after_start"] = True
                raise TaskStopped("check started")

        def observe(args, **kwargs):
            if args[-len(cancel_check):] == cancel_check:
                evidence["check_running"] = True
            if "down" in args and outcome == "interrupted":
                raise RuntimeError("controller stopped")
            output = command(args, **kwargs)
            if args[-3:] == ["config", "--format", "json"]:
                evidence["model"] = json.loads(output)
                evidence["folder"] = Path(args[args.index("--project-directory") + 1])
                evidence["workspace"] = kwargs["env"]["MASF_WORKSPACE"]
            if "up" in args:
                model = evidence["model"]
                name = model["name"]
                image = f"{name}-app" if build else None
                evidence["before"] = self.inventory(name, image)
                evidence["containers"] = {
                    item["Config"]["Labels"]["com.docker.compose.service"]: item
                    for item in self.inspect(*evidence["before"]["container"])
                }
                evidence["networks"] = self.inspect(*evidence["before"]["network"])
                evidence["app_id"] = evidence["containers"]["app"]["Id"]
            return output

        with (
            chdir(self.source),
            patch("masf.checks.command", side_effect=observe),
            patch.object(worker, "guard", side_effect=guard),
        ):
            if outcome == "cancelled":
                with self.assertRaisesRegex(TaskStopped, "check started"):
                    worker.test(sha)
            elif outcome == "interrupted":
                with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                    worker.test(sha)
            else:
                worker.test(sha)

        folder = evidence["folder"]
        record = json.loads((folder / "checks.json").read_text())
        model = evidence["model"]
        name = model["name"]
        image = f"{name}-app" if build else None
        recovery = None
        if outcome == "interrupted":
            self.assertTrue((folder / "cleanup.json").is_file())
            recovery = self.inventory(name, image)
            worker.recover()
        after = self.inventory(name, image)
        before = evidence["before"]
        cleanup_pending = (folder / "cleanup.json").exists()
        print(json.dumps({
            "attempt": outcome, "profile": "build" if build else "mount",
            "sha": record["sha"], "service": record["service"],
            "profile_sha256": record["profile_sha256"], "project": name,
            "workspace": evidence["workspace"], "image_tag": image,
            "before": before, "before_recovery": recovery, "after": after,
            "cleanup_pending": cleanup_pending,
        }, sort_keys=True), flush=True)

        self.assertFalse(cleanup_pending)
        self.assertFalse(any(after.values()), after)
        if recovery is not None:
            self.assertEqual(recovery, before)
        self.assertEqual(record["sha"], sha)
        workspace = Path(evidence["workspace"])
        for filename in (".env", "compose.yaml", "compose.override.yaml"):
            self.assertEqual((workspace / filename).read_bytes(),
                             (self.source / filename).read_bytes())
        self.assertEqual(record["name"], name)
        self.assertEqual(record["service"], "app")
        self.assertEqual(record["profile_sha256"], hashlib.sha256(
            json.dumps(model, sort_keys=True).encode()
        ).hexdigest())
        self.assertEqual(record["passed"], outcome in {"passed", "interrupted"})
        observed = record["commands"]
        self.assertEqual([item["code"] for item in observed[:5]], [0] * 5)
        self.assertEqual([observed[i]["log"] for i in (1, 2, 4)], [revision, "0", "1"])
        if outcome == "failed":
            self.assertEqual(record["failure_kind"], "functional")
            self.assertEqual(record["commands"][-1]["code"], 7)
        if outcome == "cancelled":
            self.assertTrue(evidence["cancelled_after_start"])
            self.assertEqual(record["failure_kind"], "cancelled")
            self.assertEqual(record["commands"][-1]["argv"], cancel_check)

        self.assert_deployment(evidence, build, shared_image, image)
        return {"name": name, "workspace": evidence["workspace"], "volumes": before["volume"]}

    def assert_deployment(self, evidence, build, shared_image, image):
        model = evidence["model"]
        before = evidence["before"]
        self.assertEqual(set(model["services"]), {"app", "db"})
        self.assertEqual(set(model["networks"]), {"default"})
        self.assertTrue(model["networks"]["default"]["internal"])
        self.assertEqual(set(model["volumes"]), {"data", "app-data"})
        volumes = {key: value["name"] for key, value in model["volumes"].items()}
        self.assertEqual(before["volume"], sorted(volumes.values()))
        self.assertEqual(len(before["network"]), 1)
        network = evidence["networks"][0]
        self.assertTrue(network["Internal"])
        self.assertEqual(network["Name"], model["networks"]["default"]["name"])
        self.assertEqual(len(before["container"]), 2)
        app, db = (evidence["containers"][service] for service in ("app", "db"))
        for container in (app, db):
            self.assertTrue(container["State"]["Running"])
            self.assertEqual(set(container["NetworkSettings"]["Networks"]), {network["Name"]})
        self.assertEqual(model["services"]["db"]["image"], POSTGRES)
        self.assertEqual(db["Image"], shared_image)
        self.assertEqual(
            [(m["Type"], m["Name"], m["Destination"]) for m in db["Mounts"]],
            [("volume", volumes["data"], "/var/lib/postgresql/data")],
        )
        self.assertEqual(
            [(m["Name"], m["Destination"]) for m in app["Mounts"] if m["Type"] == "volume"],
            [(volumes["app-data"], "/var/lib/postgresql/data")],
        )
        binds = [
            (m["Source"], m["Destination"], m["RW"])
            for m in app["Mounts"] if m["Type"] == "bind"
        ]
        app_model = model["services"]["app"]
        model_binds = [m for m in app_model["volumes"] if m["type"] == "bind"]
        if build:
            self.assertEqual(app_model["build"]["context"], evidence["workspace"])
            self.assertEqual(app["Config"]["Image"], image)
            self.assertEqual(before["project_image"], [app["Image"]])
            self.assertFalse(model_binds)
            self.assertFalse(binds)
        else:
            self.assertEqual(app_model["image"], POSTGRES)
            self.assertEqual(app["Image"], shared_image)
            self.assertEqual(
                [(m["source"], m["target"], m["read_only"]) for m in model_binds],
                [(evidence["workspace"], "/workspace", True)],
            )
            self.assertEqual(binds, [(evidence["workspace"], "/workspace", False)])

    def test_private_mount_profile(self):
        self.exercise_profiles(build=False)

    def test_private_build_profile(self):
        self.exercise_profiles(build=True)


class ComposeCleanupCommand(unittest.TestCase):
    def test_only_build_profiles_request_local_image_cleanup(self):
        for build in (False, True):
            with self.subTest(build=build), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workspace = root / "workspace"
                workspace.mkdir()
                app = {"build": "${MASF_WORKSPACE}"} if build else {"image": POSTGRES}
                (root / "profile.yaml").write_text(json.dumps({"services": {"app": app}}))
                repo = {
                    "compose": {"files": ["profile.yaml"], "service": "app"},
                    "setup": [],
                    "checks": [["true"]],
                }
                service = {"build": {"context": str(workspace)}} if build else app
                model = {"services": {"app": service}}
                evidence = root / "evidence"

                def docker(args, **kwargs):
                    if "config" in args:
                        return json.dumps(model)
                    if "down" in args:
                        raise RuntimeError("interrupted cleanup")
                    return ""

                with patch("masf.checks.command", side_effect=docker):
                    with self.assertRaisesRegex(RuntimeError, "interrupted cleanup"):
                        verify(repo, root / "config.toml", workspace, "sha", evidence, "test", 30)
                saved_stop, saved_workspace = json.loads((evidence / "cleanup.json").read_text())
                expected = ["down", "--volumes", "--remove-orphans"]
                if build:
                    expected.extend(["--rmi", "local"])
                self.assertEqual(saved_stop[saved_stop.index("down"):], expected)
                self.assertEqual(saved_workspace, str(workspace))
