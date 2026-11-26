import concurrent.futures
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import yaml

from masf import cli, observe, queue
from masf.evidence import digest, explanation, model
from masf.observation_api import DEFAULT_PORT, ObservationServer, connect_readonly
from masf.system import ProcessError
from tests.test_observation import BASE, CANDIDATE, NEW_HEAD, PILOT_HEAD, REPO, SLUG
from tests.test_observation import GitHub, check, job, pull, run


PROJECT = Path(__file__).resolve().parents[1]
JOB = "/v1/jobs/7/10"
PULL = "/v1/pulls/7/23"
CONFIG = f'''
version = 1
[service]
max_build_rounds = 2
[[repositories]]
slug = "{SLUG}"
enabled = true
label = "masf:ready"
private_option = "registry-value"
[[repositories]]
slug = "unused/project"
enabled = false
'''


def request(port, path, method="GET", headers=None):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=6)) as connection:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.headers, response.read()


class ObservationAPI(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="masf-i5-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.root = self.directory / "state" / "masf"
        self.config = self.directory / "registry.toml"
        self.config.write_text(CONFIG)
        self.db = queue.connect(self.root)
        self.addCleanup(self.db.close)
        self.server = ObservationServer(self.root, self.config, 0)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        self.job = self.insert()

    def insert(self, number=10, state="building", **fields):
        entry = {
            "repo_id": 7, "number": number, "slug": SLUG, "state": state,
            "base": BASE, "candidate": CANDIDATE, "fingerprint": "scope", "title": "Tarea",
        } | fields
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)",
                            (7, number, SLUG, state, json.dumps(entry)))
        return entry

    def get(self, path, expected=200):
        status, headers, body = request(self.port, path)
        self.assertEqual(status, expected, body)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        result = json.loads(body)
        self.assertEqual(result["version"], 1)
        return result

    def test_cli_uses_default_port_without_binding_the_operational_port(self):
        with patch.object(sys, "argv", ["masf", "--config", str(self.config), "observe-api"]), \
                patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root.parent)}), \
                patch.object(cli, "serve_observation") as serve:
            self.assertEqual(cli.main(), 0)
        serve.assert_called_once_with(self.root, self.config, DEFAULT_PORT, None)

    def test_cli_loopback_custom_port_with_readonly_files(self):
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.config.chmod(0o400)
        self.root.chmod(0o500)
        self.addCleanup(self.root.chmod, 0o700)
        (self.root / "queue.sqlite3").chmod(0o400)
        env = {"PATH": os.environ["PATH"], "XDG_STATE_HOME": str(self.root.parent)}
        process = subprocess.Popen(
            [sys.executable, "-m", "masf.cli", "--config", str(self.config), "observe-api", "--port", str(port)],
            cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertIn(f"http://127.0.0.1:{port}/v1", process.stdout.readline(),
                          process.stderr.read() if process.poll() is not None else "")
            status, _, body = request(port, "/v1/repositories")
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["data"][0]["slug"], "unused/project")
            with closing(socket.create_connection(("127.0.0.1", port), timeout=1)):
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.2", port), timeout=1)
        finally:
            process.terminate()
            process.communicate(timeout=5)
        self.assertFalse((self.root / "worker.lock").exists())
        absent = self.directory / "missing-state"
        result = subprocess.run(
            [sys.executable, "-m", "masf.cli", "--config", str(self.config), "observe-api"],
            cwd=PROJECT, env=env | {"XDG_STATE_HOME": str(absent)}, capture_output=True,
            text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("La cola de observación debe existir", result.stderr)
        self.assertFalse(absent.exists())

    def test_observer_container_limits_data_and_privileges(self):
        service = yaml.safe_load((PROJECT / "examples/docker/observer.yaml").read_text())["services"]["observer"]
        self.assertEqual(service["network_mode"], "host")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(set(service["environment"]), {"XDG_STATE_HOME", "PYTHONDONTWRITEBYTECODE"})
        self.assertEqual({mount["target"] for mount in service["volumes"]}, {"/etc/masf/config.toml", "/var/lib/masf"})
        self.assertTrue(all(mount["read_only"] for mount in service["volumes"]))

    def test_immediate_writer_transaction_keeps_reads_coherent_then_commit_is_visible(self):
        old = self.get(JOB)["data"]
        with closing(connect_readonly(self.root)) as reader:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                reader.execute("DELETE FROM jobs")
        self.db.execute("BEGIN IMMEDIATE")
        queue.write_event(self.db, self.job, "checking", {"candidate": NEW_HEAD})
        changed = self.job | {"candidate": NEW_HEAD, "state": "checking"}
        self.db.execute("UPDATE jobs SET state=?,data=? WHERE number=10", ("checking", json.dumps(changed)))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(self.get, JOB) for _ in range(16)]
                self.assertTrue(all(future.result()["data"] == old for future in futures))
        finally:
            self.db.commit()
        result = self.get(JOB)["data"]
        self.assertEqual((result["state"], result["candidate_sha"]), ("checking", NEW_HEAD))
        self.assertNotEqual(result["revision"], old["revision"])
        self.assertEqual(self.get(JOB + "/events")["data"][0]["id"], result["last_event_id"])

    def test_filters_pagination_and_historical_schema_projection(self):
        self.insert(11, "blocked", reason="No hay acceso, password=private-historical",
                    body="raw-issue-prompt", private_payload="opaque-extra",
                    review={"provider": "cursor", "model": "historical-alias", "sha": BASE,
                            "result": {"version": 1, "verdict": "pass", "findings": [],
                                       "access_token": "opaque-result"}})
        self.insert(12, "blocked")
        with self.db:
            self.db.execute("DROP TABLE stage_attempts")
            self.db.execute("DROP TABLE pull_requests")
        page = self.get("/v1/jobs?slug=YULOMA%2Fmasf&state=blocked&limit=1")
        self.assertEqual(page["data"][0]["number"], 11)
        self.assertEqual(page["pagination"], {"limit": 1, "offset": 0, "next_offset": 1})
        next_page = self.get("/v1/jobs?state=blocked&offset=1&limit=1")
        self.assertEqual(next_page["data"][0]["number"], 12)
        self.assertIsNone(next_page["pagination"]["next_offset"])
        attempts = self.get("/v1/jobs/7/11/attempts?role=review")["data"]
        self.assertEqual(len(attempts), 1)
        legacy = attempts[0]
        self.assertTrue(legacy["historical"])
        self.assertEqual(legacy["legacy_model"], "historical-alias")
        self.assertEqual(legacy["model"]["availability"], "unknown")
        self.assertIsNone(legacy["model"]["effective"])
        self.assertEqual(self.get("/v1/jobs/7/11/attempts/" + legacy["id"])["data"], legacy)
        self.assertEqual(self.get("/v1/pulls")["data"], [])
        repositories = self.get("/v1/repositories")["data"]
        self.assertIsNone(repositories[0]["repo_id"])
        self.assertEqual(repositories[1]["repo_id"], 7)
        for private in ("private-historical", "raw-issue-prompt", "opaque-extra", "opaque-result", "registry-value"):
            self.assertNotIn(private, json.dumps([page, next_page, attempts, repositories]))
        for path in ("/v1/jobs?offset=-1", "/v1/jobs?limit=101", "/v1/jobs?state=building&state=queued",
                     "/v1/jobs?unexpected=1", "/v1/jobs?slug=%FF"):
            self.assertEqual(self.get(path, 400)["error"]["code"], "invalid_request")
        self.assertEqual(self.get("/v1/jobs/7/999", 404)["error"]["code"], "not_found")

    def stage(self, ident, role="build", **fields):
        return {
            "id": ident, "role": role, "repo_id": 7, "number": 10, "status": "active",
            "provider": "codex", "executor": "codex", "execution_id": "build-session",
            "execution_id_source": "codex:thread_id", "cli_version": "1.0",
            "input_sha": CANDIDATE, "model": model("auto"), "provenance": {"configuration": {"secret": "internal"}},
            "evidence_file": f"{role}-{ident}.json",
            "explanation": explanation(evidence=[f"{role}-{ident}.json"]),
        } | fields

    def evidence_folder(self, number=10):
        folder = self.root / "evidence" / f"7-{number}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def test_registered_artifacts_are_scoped_filtered_and_requests_preserve_installation(self):
        folder = self.evidence_folder()
        other = self.evidence_folder(11)
        self.insert(11)
        stage = self.stage("builder", status="completed", result={"summary": "ok", "secret": "never-output"})
        self.assertTrue(queue.claim(self.db, self.job, "internal-lease-secret", 60))
        queue.record_stage(self.db, self.job, stage, boundary="finished")
        (folder / stage["evidence_file"]).write_text(json.dumps(stage))
        path = "build-builder.activity.jsonl"
        stage["explanation"]["evidence"].extend([path, "../7-11/private.json"])
        queue.record_stage(self.db, self.job, stage)
        event = {"attempt": "builder", "sha": CANDIDATE, "event": "provider_activity", "raw": "raw-input"}
        (folder / path).write_text(json.dumps(event) + "\n" + '{"part')
        (other / "private.json").write_text("other-work-data")
        (folder / "build-linked.json").symlink_to(other / "private.json")
        (folder / "build-directory.json").mkdir()
        os.mkfifo(folder / "build-pipe.json")
        os.link(other / "private.json", folder / "build-hardlinked.json")
        for ident in ("linked", "directory", "pipe", "hardlinked"):
            queue.record_stage(self.db, self.job, self.stage(ident))
        workspace = self.directory / "jobs" / "7-10"
        workspace.mkdir(parents=True)
        (workspace / "work.txt").write_text("working files")

        def content():
            return {item.relative_to(self.directory): item.read_bytes()
                    for item in self.directory.rglob("*") if item.is_file()}

        before = content()
        result = self.get(JOB + "/attempts/builder")["data"]
        refs = result["explanation"]["evidence"]
        self.assertEqual(len(refs), 2)
        path = JOB + "/artifacts/" + refs[0]["artifact_id"]
        evidence = self.get(path)
        self.assertEqual(evidence["data"]["content"]["result"]["summary"], "ok")
        self.assertNotIn("never-output", json.dumps(evidence))
        self.assertNotIn("internal", json.dumps(result))
        self.assertEqual(request(self.port, path, "HEAD")[::2], (200, b""))
        events = self.get(JOB + "/artifacts/" + refs[1]["artifact_id"] + "?limit=1")["data"]
        self.assertEqual(events[0]["event"], "provider_activity")
        self.assertNotIn("raw-input", json.dumps(events))
        self.get(path.replace("/7/10/", "/7/11/"), 404)
        self.get(JOB + "/artifacts/../7-11/private.json", 400)
        self.get(JOB + "/artifacts/%2e%2e%2f7-11%2fprivate.json", 400)
        forged = digest([(7, 10), "builder", "../7-11/private.json"])
        self.get(JOB + "/artifacts/" + forged, 404)
        for ident in ("linked", "directory", "pipe", "hardlinked"):
            ref = self.get(JOB + "/attempts/" + ident)["data"]["explanation"]["evidence"][0]
            self.get(JOB + "/artifacts/" + ref["artifact_id"], 404)
        response = request(self.port, JOB, "POST")
        self.assertEqual(response[0], 405)
        self.assertEqual(json.loads(response[2])["error"]["code"], "method_not_allowed")
        response = request(self.port, JOB, headers={"Host": "outside.example"})
        self.assertEqual(response[0], 400)
        self.assertEqual(content(), before)
        self.assertFalse((self.root / "worker.lock").exists())

    def test_attempts_identity_and_explanation_survive_retry_checks_and_operator(self):
        self.job["fingerprint"] = queue.fingerprint({"title": "Tarea", "body": "Alcance"})
        repo = {"slug": SLUG, "label": "ready", "enabled": True, "routing": {"build": "codex"},
                "protected_paths": [".github/workflows/**"]}
        self.job.update(repo=repo, branch="main")
        queue.save(self.db, self.job, "building")
        self.assertTrue(queue.claim(self.db, self.job, "lease-first", 60))
        first = self.stage("first", lease="lease-first")
        queue.record_stage(self.db, self.job, first, boundary="started")
        active = self.get(JOB)["data"]["active_attempt"]
        self.assertEqual((active["id"], active["executor"]), ("first", "codex"))
        self.assertEqual(active["explanation"]["availability"], "pending")
        self.assertEqual(active["model"]["availability"], "pending")
        first["model"] = {"requested": "auto", "effective": "codex-effective", "source": "codex:cli",
                          "availability": "reported"}
        queue.record_stage(self.db, self.job, first)
        active = self.get(JOB + "/attempts/first")["data"]
        self.assertEqual(active["model"]["effective"], "codex-effective")
        self.assertEqual(active["explanation"]["availability"], "pending")
        first.update(status="failed", error="timeout",
                     explanation=explanation("Timeout", reason="timeout", evidence=[first["evidence_file"]]))
        queue.record_stage(self.db, self.job, first, boundary="finished")
        with self.db:
            self.db.execute("DELETE FROM leases WHERE token='lease-first'")
        finished = self.get(JOB + "/attempts/first")["data"]
        self.assertEqual((finished["id"], finished["explanation"]["availability"]), ("first", "available"))
        self.assertEqual(finished["status"], "failed")

        second = self.stage(
            "second", executor="cursor", provider="cursor", execution_id="cursor-run",
            execution_id_source="cursor:session", cli_version="2.0", status="completed",
            model={"requested": "fast", "effective": "cursor-effective", "source": "cursor:cli", "availability": "reported"},
            usage={"availability": "reported", "measurements": [
                {"name": "outputTokens", "value": 0, "unit": "tokens", "source": "cursor:tokens"},
                {"name": "cost", "value": .02, "unit": "USD", "source": "cursor:estimate", "kind": "estimated"},
                {"name": "cost", "value": .01, "unit": "USD", "source": "cursor:billed", "kind": "confirmed"},
            ]},
            previous_attempt="first", retry_of="first", candidate_sha=NEW_HEAD,
            explanation=explanation("Cambios", criteria=["AC1"], decisions=["Reparar"],
                                    evidence=["build-second.json", "first"]),
        )
        queue.record_stage(self.db, self.job, second, boundary="finished")
        report = {"sha": NEW_HEAD, "passed": True, "commands": [{"code": 0, "argv": ["check", "private-arg"]}]}
        check_path = "a" * 32 + "/checks.json"
        checks_folder = self.evidence_folder() / ("a" * 32)
        checks_folder.mkdir()
        (checks_folder / "checks.json").write_text(json.dumps(report))
        local = self.stage("local", "checks", executor="masf", provider=None, execution_id=None,
                           execution_id_source=None, cli_version=None, status="completed", result=report,
                           input_sha=NEW_HEAD, explanation=explanation("Check", evidence=[check_path]))
        queue.record_stage(self.db, self.job, local, boundary="finished")
        question = self.stage("question", "plan", status="needs_input", result={"question": "¿Continuar?"})
        queue.record_stage(self.db, self.job, question, boundary="finished")
        queue.save(self.db, self.job, "needs_input", reason="¿Continuar?", plan_attempt="question",
                   checks=report, feedback={"summary": "Consulta previa", "raw": "private-feedback"})
        waiting = self.get(JOB)["data"]
        self.assertEqual(waiting["intervention"]["questions"], [{"attempt_id": "question", "text": "¿Continuar?"}])
        self.assertTrue(waiting["intervention"]["pending"])
        self.assertEqual(waiting["checks"]["sha"], NEW_HEAD)
        issue = {
            "title": "Tarea", "body": "Alcance", "state": "open", "labels": [{"name": "ready"}],
        }
        with patch("masf.cli.api", return_value=issue):
            cli.resume(self.db, {"repositories": [repo], "service": {"max_build_rounds": 2}}, SLUG + "#10",
                       evidence="Alcance confirmado")
        resumed = self.get(JOB)["data"]
        self.assertEqual(resumed["state"], "planning")
        self.assertFalse(resumed["intervention"]["pending"])
        self.assertEqual(resumed["intervention"]["questions"], [])
        self.assertNotEqual(waiting["revision"], resumed["revision"])
        self.assertEqual(resumed["revision"], self.get(JOB)["data"]["revision"])
        events = self.get(JOB + "/events")["data"]
        self.assertEqual(events[-2]["feedback"]["summary"], "Consulta previa")
        operation = events[-1]["operations"][0]
        self.assertEqual(operation["decision"]["evidence"], "Alcance confirmado")
        self.assertEqual(operation["model"]["availability"], "not_applicable")
        self.assertEqual(events[-1]["state"], "planning")

        first, second = self.get(JOB + "/attempts?role=build")["data"]
        self.assertEqual((first["provider"], second["provider"]), ("codex", "cursor"))
        self.assertEqual((first["cli_version"], second["cli_version"]), ("1.0", "2.0"))
        self.assertEqual(first["model"]["effective"], "codex-effective")
        self.assertEqual(second["execution_id_source"], "cursor:session")
        self.assertEqual(second["model"]["effective"], "cursor-effective")
        self.assertEqual(second["retry_of"], first["id"])
        self.assertEqual(second["explanation"]["evidence"][1]["sha"], CANDIDATE)
        self.assertEqual(second["explanation"]["criteria"], ["AC1"])
        measurements = second["usage"]["measurements"]
        self.assertEqual(measurements[0]["value"], 0)
        self.assertEqual(measurements[0]["source"], "cursor:tokens")
        self.assertEqual([entry["kind"] for entry in measurements], [None, "estimated", "confirmed"])
        self.assertEqual([entry["unit"] for entry in measurements], ["tokens", "USD", "USD"])
        local = self.get(JOB + "/attempts/local")["data"]
        self.assertEqual(local["model"]["availability"], "not_applicable")
        ref = local["explanation"]["evidence"][1]["artifact_id"]
        artifact = self.get(JOB + "/artifacts/" + ref)["data"]
        self.assertEqual(artifact["content"]["sha"], NEW_HEAD)
        self.assertEqual(artifact["content"]["commands"][0]["code"], 0)
        self.assertNotIn("private-", json.dumps([artifact, events]))

        current = json.loads(self.db.execute("SELECT data FROM jobs WHERE number=10").fetchone()["data"])
        protected = ".github/workflows/check.yml"
        queue.save(self.db, current, "blocked", reason="Ruta protegida", candidate=NEW_HEAD,
                   retry="checking", blocked_paths=[protected])
        with (patch("masf.cli.api", return_value=issue),
              patch("masf.actions.command", return_value=f"1\t0\t{protected}\0")):
            cli.resume(self.db, {"repositories": [repo], "service": {"max_build_rounds": 2}}, SLUG + "#10", allow_protected_paths=[protected])
        authorization = self.get(JOB + "/events")["data"][-1]
        self.assertEqual(authorization["operations"][0]["decision"]["paths"], [protected])
        self.assertEqual(authorization["path_approvals"][0]["path"], protected)
        self.assertEqual(authorization["state"], "checking")

    def test_pr_ci_reports_errors_separately_from_known_results_and_retains_old_head(self):
        self.job.update(pr_number=23, published_head=PILOT_HEAD, candidate=PILOT_HEAD)
        queue.save(self.db, self.job, "pr_open")
        github = GitHub()
        github.checks[PILOT_HEAD] = [check(11, "completed", "failure")]
        github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 1, "completed", "failure")]
        github.jobs[80, 1] = [job(51, 11, PILOT_HEAD, 1, "completed", "failure")]
        cfg = {"repositories": [{"slug": SLUG, "enabled": True}]}
        with patch("masf.queue.command", side_effect=github.command):
            self.assertEqual(observe.observe(self.db, queue.api, cfg, {SLUG: 7}), 0)
            snapshot = self.get(PULL)["data"]
            original = snapshot["ci"]
            endpoint = f"{REPO}/commits/{PILOT_HEAD}/check-runs"
            github.failures[endpoint, 1] = ProcessError("temporary", "HTTP/2 503 Unavailable\n\n{}", "HTTP 503")
            self.assertGreater(observe.observe(self.db, queue.api, cfg, {SLUG: 7}), 0)
            failed_poll = self.get(PULL)["data"]["ci"]
            self.assertEqual(failed_poll["checks"], original["checks"])
            self.assertEqual(failed_poll["last_success_at"]["check_runs"], original["last_success_at"]["check_runs"])
            self.assertGreaterEqual(failed_poll["observed_at"], original["observed_at"])
            self.assertEqual(failed_poll["errors"]["check_runs"]["status"], 503)
            github.pulls = [[pull(sha=NEW_HEAD)]]
            github.checks[NEW_HEAD] = [check(12, "completed", "success")]
            self.assertEqual(observe.observe(self.db, queue.api, cfg, {SLUG: 7}), 0)
        self.assertEqual(self.get("/v1/pulls?repo_id=7&job_number=10&sha=" + NEW_HEAD)["data"][0]["head"]["sha"], NEW_HEAD)
        history = self.get(PULL + "/ci?sha=" + PILOT_HEAD)["data"][0]
        self.assertEqual((history["repo_id"], history["number"], history["job_number"], history["sha"]),
                         (7, 23, 10, PILOT_HEAD))
        self.assertEqual(history["runs"]["80:1"]["run"]["conclusion"], "failure")
        failed = history["checks"]["check:11"]
        self.assertEqual((failed["run_id"], failed["attempt"], failed["job_id"]), (80, 1, 51))
        self.assertIn("/actions/jobs/51/logs", failed["diagnostic"]["log_url"])
        self.assertIn("[redacted]", failed["diagnostic"]["log_excerpt"])
        self.assertNotIn("opaque-fixture", json.dumps(history))
        events = self.get(PULL + "/events?sha=" + PILOT_HEAD)["data"]
        self.assertEqual({event["sha"] for event in events}, {PILOT_HEAD})
        self.assertIn("observation_error", {event["kind"] for event in events})
        queue.save(self.db, self.job, "blocked", reason="CI requiere autorización", ci_intervention=True,
                   retry="checking", ci_repair={"id": "repair-1", "sha": PILOT_HEAD, "pr": 23,
                   "decision": "Esperar al operador", "counters": {"build_limit": 2, "technical_limit": 2},
                   "diagnostics": [{"kind": "functional", "sources": [endpoint],
                                    "check": {"id": 51, "run": 80, "attempt": 1}}]})
        repair = self.get(JOB)["data"]
        self.assertTrue(repair["intervention"]["ci"])
        self.assertEqual(repair["intervention"]["retry_stage"], "checks")
        self.assertEqual(repair["repair"]["limits"]["build_limit"], 2)
        self.assertEqual(repair["repair"]["diagnostics"][0]["sources"], ["https://api.github.com/" + endpoint])


if __name__ == "__main__":
    unittest.main()
