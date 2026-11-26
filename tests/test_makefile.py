"""Exercise operational recipes with real temporary Git repos and fake Docker."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("make"), "requires GNU Make")
class FactoryRecipes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="masf recipes ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.installation = self.root / "installation"
        for name in ("config", "state", "jobs"):
            (self.installation / name).mkdir(parents=True)
        self.config = self.installation / "config/pilot.toml"
        self.config.write_text("# Private registry preserved during updates\n")
        self.state = self.installation / "state/existing.sqlite"
        self.state.write_text("existing state")
        self.log = self.root / "commands.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("MASF_", "GIT_"))
        }
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            MASF_ROOT=str(self.installation),
            MASF_PROVIDERS="codex",
            MASF_DOCKER_GID="0",
            MASF_TEST_LOG=str(self.log),
            GIT_AUTHOR_NAME="Recipe test",
            GIT_AUTHOR_EMAIL="recipe@example.invalid",
            GIT_COMMITTER_NAME="Recipe test",
            GIT_COMMITTER_EMAIL="recipe@example.invalid",
            GH_TOKEN="test-only",
        )
        for name in ("docker", "codex"):
            self.stub(self.bin / name)
        self.stub(self.installation / "control")
        for name in ("Makefile", "scripts/factory.sh", "examples/docker/build-runner.sh"):
            destination = self.checkout / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        self.git("init", "--initial-branch=main")
        self.git("add", ".")
        self.git("commit", "-m", "Initial operational recipes")
        self.remote = self.root / "origin.git"
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", "main")

    def stub(self, path):
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "with open(os.environ['MASF_TEST_LOG'], 'a') as log:\n"
            "    log.write(json.dumps({'command': Path(sys.argv[0]).name,\n"
            "        'args': sys.argv[1:], 'config': os.getenv('MASF_CONFIG_DIR')}) + '\\n')\n"
            "failure = os.getenv('MASF_TEST_FAIL')\n"
            "if failure and failure in ' '.join(sys.argv[1:]):\n"
            "    sys.exit(42)\n"
        )
        path.chmod(0o755)

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.checkout, env=self.env,
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def make(self, *args, success=True):
        result = subprocess.run(
            ["make", "--no-print-directory", *args], cwd=self.checkout,
            env=self.env, capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def upstream_commit(self):
        upstream = self.root / "upstream"
        self.git("clone", str(self.remote), str(upstream))
        (upstream / "new-revision").write_text("Remote main update\n")
        self.git("add", ".", cwd=upstream)
        self.git("commit", "-m", "New remote revision", cwd=upstream)
        self.git("push", "origin", "main", cwd=upstream)
        return self.git("rev-parse", "HEAD", cwd=upstream)

    def test_default_help_needs_no_installation_or_docker(self):
        shutil.rmtree(self.installation)
        result = self.make()
        self.assertIn("start", result.stdout)
        self.assertIn("update", result.stdout)
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.installation.exists())

    def test_recipes_reuse_existing_control(self):
        expected = {
            "start": ["up", "-d", "controller"],
            "stop": ["stop", "controller"],
            "restart": ["up", "-d", "--force-recreate", "controller"],
            "ps": ["ps", "controller"],
            "status": ["exec", "-T", "controller", "masf", "--config", "/etc/masf/pilot.toml", "status"],
            "logs": ["logs", "--tail", "100", "controller"],
            "logs-follow": ["logs", "--tail", "100", "--follow", "controller"],
            "doctor": ["run", "--rm", "--no-deps", "controller", "doctor"],
            "doctor-offline": ["run", "--rm", "--no-deps", "controller", "doctor", "--offline"],
            "scan": ["run", "--rm", "--no-deps", "controller", "scan", "--dry-run"],
        }
        for recipe, args in expected.items():
            with self.subTest(recipe=recipe):
                self.make(recipe)
                self.assertEqual(self.commands()[-1]["command"], "control")
                self.assertEqual(self.commands()[-1]["args"], args)

    def test_explicit_wrapper_override(self):
        custom = self.root / "custom control"
        self.stub(custom)
        self.make("start", f"MASF_CONTROL={custom}")
        self.assertEqual(self.commands()[0]["command"], "custom control")

    def test_compose_fallback_uses_existing_paths_and_explicit_engine(self):
        (self.installation / "control").unlink()
        self.make("start", "MASF_PROJECT=test-control", "MASF_DOCKER_SOCKET=/test/socket")
        command = self.commands()[0]
        self.assertEqual(command["command"], "docker")
        self.assertEqual(command["args"][:7], ["--host", "unix:///test/socket", "compose", "--env-file", "/dev/null", "-p", "test-control"])
        self.assertEqual(command["config"], str(self.installation / "config"))
        self.assertIn(str(self.checkout / "examples/docker/engine.yaml"), command["args"])
        self.assertEqual(command["args"][-3:], ["up", "-d", "controller"])

    def test_missing_installation_fails_without_creating_one(self):
        shutil.rmtree(self.installation)
        self.make("start", success=False)
        self.assertFalse(self.installation.exists())
        self.assertEqual(self.commands(), [])

    def test_update_installs_fetched_main_and_preserves_registry_and_state(self):
        revision = self.upstream_commit()
        self.make("update", "MASF_DOCKER_SOCKET=/test/socket")
        self.assertEqual(self.git("rev-parse", "HEAD"), revision)
        commands = self.commands()
        self.assertIn("MASF_REVISION=" + revision, commands[0]["args"])
        self.assertEqual([c["command"] for c in commands], ["docker"] * 3 + ["control"] * 4)
        self.assertTrue(all(c["args"][:2] == ["--host", "unix:///test/socket"] for c in commands[:3]))
        self.assertIn("controller", commands[0]["args"])
        self.assertIn("runtime", commands[1]["args"])
        self.assertIn("masf-runner:pilot", commands[2]["args"])
        self.assertEqual(commands[3]["args"][-2:], ["doctor", "--offline"])
        self.assertEqual(commands[4]["args"], ["up", "-d", "--force-recreate", "controller"])
        self.assertEqual(self.state.read_text(), "existing state")
        self.assertEqual(self.config.read_text(), "# Private registry preserved during updates\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_update_refuses_feature_branch_and_detached_head(self):
        for args in (("switch", "-c", "feature"), ("checkout", "--detach")):
            with self.subTest(args=args):
                self.git(*args)
                result = self.make("update", success=False)
                self.assertIn("checkout de main", result.stderr)
        self.assertEqual(self.commands(), [])

    def test_update_refuses_tracked_and_untracked_changes(self):
        for name in ("Makefile", "untracked"):
            with self.subTest(name=name):
                path = self.checkout / name
                previous = path.read_bytes() if path.exists() else None
                with path.open("a") as changed:
                    changed.write("\n# Local change\n")
                result = self.make("update", success=False)
                self.assertIn("cambios locales", result.stderr)
                if previous is None:
                    path.unlink()
                else:
                    path.write_bytes(previous)
        self.assertEqual(self.commands(), [])

    def test_update_refuses_local_commits_even_when_fast_forward_is_possible(self):
        self.git("commit", "--allow-empty", "-m", "Local only")
        head = self.git("rev-parse", "HEAD")
        result = self.make("update", success=False)
        self.assertIn("commits ajenos", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.commands(), [])

    def test_update_refuses_divergent_main(self):
        self.upstream_commit()
        self.test_update_refuses_local_commits_even_when_fast_forward_is_possible()

    def test_update_does_not_recreate_after_build_or_validation_failure(self):
        for failure in ("--target controller", "--target runtime", "Runner.Dockerfile", "doctor --offline"):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                self.env["MASF_TEST_FAIL"] = failure
                self.make("update", success=False)
                self.assertFalse(any("up" in c["args"] for c in self.commands()))
                self.assertEqual(self.state.read_text(), "existing state")

    def test_update_does_not_build_after_fetch_failure(self):
        self.git("remote", "set-url", "origin", str(self.root / "missing.git"))
        self.make("update", success=False)
        self.assertEqual(self.commands(), [])
