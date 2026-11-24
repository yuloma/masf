import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parent / "examples/docker/smoke-profile.sh"


class SmokeProfileArgumentsTest(unittest.TestCase):
    def run_without_docker(self, argument):
        with tempfile.TemporaryDirectory() as directory:
            docker = Path(directory) / "docker"
            log = Path(directory) / "docker.log"
            docker.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$DOCKER_INVOCATIONS"\n'
                'exit 99\n',
                encoding="utf-8",
            )
            docker.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = directory + os.pathsep + os.environ.get("PATH", os.defpath)
            env["DOCKER_INVOCATIONS"] = str(log)
            result = subprocess.run(
                ["bash", str(SCRIPT), argument],
                capture_output=True,
                encoding="utf-8",
                env=env,
                timeout=10,
            )
            if log.exists():
                self.fail("Unexpected Docker invocation: " + log.read_text(encoding="utf-8"))
            return result

    def assert_help(self, argument):
        result = self.run_without_docker(argument)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("Uso: bash examples/docker/smoke-profile.sh", result.stdout)
        self.assertIn("el candidato empieza vacío", " ".join(result.stdout.split()))
        self.assertIn("la base conserva su fila de control", result.stdout)
        self.assertIn("MASF_DOCKER_SOCKET", result.stdout)

    def test_long_help(self):
        self.assert_help("--help")

    def test_short_help(self):
        self.assert_help("-h")

    def test_unknown_argument(self):
        argument = "--desconocido"
        result = self.run_without_docker(argument)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(argument, result.stderr)
