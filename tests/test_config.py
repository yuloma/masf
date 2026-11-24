import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SLUG = "example-org/legacy"
REPO = f"repositories[{SLUG}]"
CONFIG = {
    "version": "version = 1\n",
    "service": """[service]
poll_seconds = 60
step_timeout_seconds = 900
max_build_rounds = 2
max_parallel_jobs = 1
""",
    "runner": '[runner]\nimage = "masf-runner:local"\n',
    "providers": """[providers.codex]
enabled = true
executable = "codex"
[providers.claude]
enabled = false
executable = "claude"
""",
    "routing": '[routing]\nplan = "codex"\nbuild = "codex"\nreview = "codex"\n',
    "repositories": f"""[[repositories]]
slug = "{SLUG}"
enabled = true
label = "masf:ready"
setup = []
checks = [["true"]]
""",
}
COMPOSE = '[repositories.compose]\nfiles = ["profile.yaml"]\nservice = "app"\n'


class OfflineDoctor(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "config.toml"

    def doctor(self, path):
        return subprocess.run(
            [sys.executable, "-m", "masf.cli", "--config", str(path), "doctor", "--offline"],
            cwd=ROOT,
            env={
                "PATH": str(self.root),
                "PYTHONUTF8": "1",
                "XDG_CONFIG_HOME": str(self.root),
            },
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )

    def assert_invalid(self, config, field, detail):
        self.config.write_text(config, encoding="utf-8")
        result = self.doctor(self.config)
        self.assertEqual(result.returncode, 1, result)
        self.assertIn(f"Configuración inválida: {field}: {detail}", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_required_sections_missing_or_wrong_type(self):
        for section in ("service", "runner", "providers", "routing", "repositories"):
            data = "".join(value for name, value in CONFIG.items() if name != section)
            detail = (
                "se requiere una lista" if section == "repositories" else "se requiere una tabla"
            )
            for invalid in ("", f"{section} = false\n"):
                with self.subTest(section=section, invalid=invalid):
                    self.assert_invalid(invalid + data, section, detail)

    def test_repository_without_valid_slug(self):
        config = "".join(CONFIG.values())
        slug = f'slug = "{SLUG}"\n'
        for invalid in ("", "slug = 42\n", 'slug = "invalid"\n'):
            with self.subTest(invalid=invalid):
                self.assert_invalid(
                    config.replace(slug, invalid),
                    "repositories[0].slug",
                    "se requiere un identificador",
                )

        no_repos = "".join(value for name, value in CONFIG.items() if name != "repositories")
        self.assert_invalid(
            "repositories = [0]\n" + no_repos, "repositories[0]", "se requiere una tabla"
        )
        self.assert_invalid(
            config + CONFIG["repositories"].replace(SLUG, SLUG.upper()),
            "repositories[1].slug",
            f"el repositorio {SLUG.upper()} está repetido",
        )

    def test_fields_and_existing_constraints(self):
        config = "".join(CONFIG.values())
        cases = [
            ("version = 1", "version = true", "version", "se requiere el entero 1"),
            (
                "poll_seconds = 60",
                "poll_seconds = true",
                "service.poll_seconds",
                "se requiere un entero positivo",
            ),
            (
                "step_timeout_seconds = 900",
                "",
                "service.step_timeout_seconds",
                "se requiere un entero positivo",
            ),
            (
                "max_build_rounds = 2",
                "max_build_rounds = 0",
                "service.max_build_rounds",
                "se requiere un entero positivo",
            ),
            (
                "max_parallel_jobs = 1",
                "max_parallel_jobs = -1",
                "service.max_parallel_jobs",
                "se requiere un entero positivo",
            ),
            (
                'image = "masf-runner:local"',
                "image = []",
                "runner.image",
                "se requiere texto no vacío",
            ),
            (
                CONFIG["providers"],
                '[providers]\ncodex = 1\n',
                "providers.codex",
                "se requiere una tabla",
            ),
            (
                "[providers.codex]",
                "[providers.other]",
                "providers.other",
                "proveedor desconocido",
            ),
            (
                'executable = "codex"',
                "executable = 1",
                "providers.codex.executable",
                "se requiere texto no vacío",
            ),
            (
                'enabled = true\nexecutable',
                'enabled = "sí"\nexecutable',
                "providers.codex.enabled",
                "se requiere un booleano",
            ),
            (
                'executable = "codex"',
                'executable = "codex"\nmodel = []',
                "providers.codex.model",
                "se requiere texto",
            ),
            ('plan = "codex"', "plan = []", "routing.plan", "se requiere texto no vacío"),
            ('plan = "codex"', 'extra = "codex"', "routing.extra", "rol desconocido"),
            ('plan = "codex"', "", REPO + ".routing.plan", "falta asignar un proveedor"),
            (
                'plan = "codex"',
                'plan = "cursor"',
                REPO + ".routing.plan",
                "el proveedor asignado no está configurado",
            ),
            (
                'plan = "codex"',
                'plan = "claude"',
                REPO + ".routing.plan",
                "el proveedor asignado está deshabilitado",
            ),
            (
                'label = "masf:ready"',
                'label = "masf:ready"\nrouting = []',
                REPO + ".routing",
                "se requiere una tabla",
            ),
            (
                'label = "masf:ready"',
                'label = "masf:ready"\nrouting = {build = []}',
                REPO + ".routing.build",
                "se requiere texto no vacío",
            ),
            ('label = "masf:ready"', "", REPO + ".label", "se requiere texto no vacío"),
            (
                "enabled = true\nlabel",
                "enabled = 1\nlabel",
                REPO + ".enabled",
                "se requiere un booleano",
            ),
            ("setup = []", 'setup = ["true"]', REPO + ".setup[0]", "se requiere una lista"),
            (
                'checks = [["true"]]',
                "checks = []",
                REPO + ".checks",
                "se requiere al menos un comando",
            ),
            (
                'checks = [["true"]]',
                "checks = [[]]",
                REPO + ".checks[0]",
                "se requiere al menos un argumento",
            ),
            (
                'checks = [["true"]]',
                'checks = [["true", 1]]',
                REPO + ".checks[0][1]",
                "se requiere texto no vacío",
            ),
            (
                "setup = []",
                "setup = []\nprotected_paths = [42]",
                REPO + ".protected_paths[0]",
                "se requiere texto no vacío",
            ),
            (
                "setup = []",
                "setup = []\nbase_branch = true",
                REPO + ".base_branch",
                "se requiere texto",
            ),
            (
                "setup = []",
                "setup = []\nrunner_image = []",
                REPO + ".runner_image",
                "se requiere texto no vacío",
            ),
        ]
        for original, replacement, field, detail in cases:
            with self.subTest(field=field, replacement=replacement):
                result = config.replace(original, replacement, 1)
                self.assert_invalid(result, field, detail)

    def test_compose_block(self):
        config = "".join(CONFIG.values())
        path = REPO + ".compose"
        cases = [
            ("compose = []", path, "se requiere una tabla"),
            (
                COMPOSE.replace('["profile.yaml"]', "[]"),
                path + ".files",
                "se requiere al menos un archivo",
            ),
            (
                COMPOSE.replace('["profile.yaml"]', '["ok.yaml", 17]'),
                path + ".files[1]",
                "se requiere texto",
            ),
            (COMPOSE.replace('"app"', "42"), path + ".service", "se requiere texto"),
            (
                COMPOSE.replace('"app"', '"bad/name"'),
                path + ".service",
                "nombre de servicio inválido",
            ),
            (
                COMPOSE.replace('"profile.yaml"', '"../outside.yaml"'),
                path + ".files[0]",
                "se requiere un archivo relativo",
            ),
            (
                COMPOSE.replace('"profile.yaml"', '"/profile.yaml"'),
                path + ".files[0]",
                "se requiere un archivo relativo",
            ),
            (
                COMPOSE.replace('"profile.yaml"', '"link.yaml"'),
                path + ".files[0]",
                "se requiere un archivo relativo",
            ),
            (COMPOSE, path + ".files[0]", "se requiere un archivo relativo"),
        ]
        (self.root / "ok.yaml").write_text("services: {app: {}}", encoding="utf-8")
        (self.root / "link.yaml").symlink_to("ok.yaml")
        for block, field, detail in cases:
            with self.subTest(block=block):
                self.assert_invalid(config + block, field, detail)

    def test_compose_yaml_structure(self):
        config = "".join(CONFIG.values()) + COMPOSE
        path = REPO + ".compose"
        cases = [
            ("services: [", ".files[0]", "YAML inválido"),
            ("services: !!bool maybe", ".files[0]", "YAML inválido"),
            ("[]", ".files[0]", "se requiere una tabla"),
            ("services: app", ".files[0].services", "se requiere una tabla"),
            ("services: [app]", ".files[0].services", "se requiere una tabla"),
            ("services: {42: {}}", ".files[0].services", "se requiere texto no vacío"),
            ("services: {app: []}", ".files[0].services.app", "se requiere una tabla"),
            ("services: {other: {}}", ".service", "el servicio no figura"),
        ]
        for yaml, field, detail in cases:
            with self.subTest(yaml=yaml):
                (self.root / "profile.yaml").write_text(yaml, encoding="utf-8")
                self.assert_invalid(config, path + field, detail)

    def test_accepts_example_and_legacy_configuration(self):
        self.config.write_text("".join(CONFIG.values()), encoding="utf-8")
        for path in (ROOT / "examples/factory.toml", self.config):
            with self.subTest(path=path):
                result = self.doctor(path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertIn("codex habilitado", result.stdout)
                self.assertIn("claude deshabilitado", result.stdout)

    def test_local_routing_extends_and_overrides_global_routing(self):
        data = "".join(CONFIG.values()).replace(
            'plan = "codex"\nbuild = "codex"', 'build = "claude"'
        )
        data += '[repositories.routing]\nplan = "codex"\nbuild = "codex"\n'
        data += COMPOSE.replace('["profile.yaml"]', '["override.yaml", "profile.yaml"]')
        (self.root / "override.yaml").write_text("volumes: {}\n", encoding="utf-8")
        (self.root / "profile.yaml").write_text("services: {app: {}}", encoding="utf-8")
        self.config.write_text(data, encoding="utf-8")
        result = self.doctor(self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
