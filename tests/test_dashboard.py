"""Dashboard acceptance against loopback HTTP and the controller's temporary queue."""

import http.client
import io
import json
import os
import signal
import subprocess
import sys
import threading
import unittest
import uuid
from contextlib import closing, redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from masf import cli, observe, queue, recovery, worker
from masf.actions import ActionError, Actions, process_pending
from masf.evidence import digest, model, usage
from masf.observation_api import ObservationServer
from masf.queue import api as queue_api
from masf.system import CommandTimeout, ProcessError
from tests import test_factory as factory
from tests import test_observation as github


JOB = "/v1/jobs/123/1"
TOKEN = "dashboard-local-fixture-token-0000000000"
OPERATOR = "Operadora de prueba"


class DashboardFixture:
    def start_dashboard(self):
        config = self.root / "factory.toml"
        global_config, repo, _ = factory.CONFIG.read_text().split("[[repositories]]", 2)
        config.write_text(global_config + "[[repositories]]" + repo.replace("enabled = false", "enabled = true"))
        auth = self.root / "actions.json"
        auth.touch(mode=0o600)
        auth.write_text(json.dumps({"operator": OPERATOR, "token": TOKEN}))
        for patcher in (patch("masf.queue.api", lambda *args, **kwargs: worker.api(*args, **kwargs)),
                        patch.dict(os.environ, {"MASF_JOBS_DIR": str(self.root / "jobs")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ObservationServer(self.root / "state", config, 0, auth)
        self.port = self.server.server_port
        runner = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        runner.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(runner.join)
        self.addCleanup(self.server.shutdown)

    def request(self, path=JOB, payload=None, key=None, token=TOKEN, **headers):
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if payload is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key or uuid.uuid4().hex})
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)) as connection:
            connection.request("GET" if payload is None else "POST", path,
                               None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse()
            if response.headers["Content-Type"].startswith("application/json"):
                return response.status, json.loads(response.read())
            return response.status, response.read().decode()

    def data(self, path=JOB):
        status, result = self.request(path)
        self.assertEqual(status, 200, result)
        return result["data"]

    def context(self):
        return self.data(JOB + "/actions")

    def payload(self, kind, *, options=None, evidence="Verificado por el operador", **overrides):
        return dict(kind=kind, revision=self.data()["revision"], options=options or {},
                    evidence=evidence, confirm=True) | overrides

    def submit(self, kind, **kwargs):
        status, result = self.request(JOB + "/actions", self.payload(kind, **kwargs))
        self.assertEqual(status, 202, result)
        self.assertEqual(result["data"]["status"], "accepted")
        return result["data"]

    def receipt(self, receipt):
        return self.data(JOB + "/actions/" + receipt["id"])

    def apply_pending(self, db=None):
        return process_pending(db or self.worker.db, self.cfg, self.root / "state",
                               lookup=worker.api, latest=lambda *_: self.base)

    def scenario(self):
        self.changed = ".github/workflows/new.yml"
        self.agent_calls = []
        self.plan_calls = 0
        self.deny = True

        def agent(role, job, cfg, workspace, inputs, owner, **kwargs):
            self.agent_calls.append((role, inputs))
            report = model("auto")
            if role == "plan":
                self.plan_calls += 1
                report = model("auto") | {"effective": f"fixture-plan-{self.plan_calls}",
                                          "source": "fixture", "availability": "reported"}
                if self.plan_calls == 1:
                    self.roles.append(role)
                    return {"result": {"question": "¿Para quién es el saludo?"},
                            "model": report, "version": "fixture-1.0"}
                self.assertEqual(inputs["operator_feedback"]["instruction"], "Para Ana")
            result = self.agent(role, job, cfg, workspace, inputs, owner, **kwargs)
            return result | {"model": report, "version": "fixture-1.0",
                             "usage_evidence": usage([{"name": "output", "value": 0, "unit": "tokens",
                                                       "source": "fixture"}])}

        patcher = patch("masf.providers.invoke", agent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_ready(self):
        self.assertEqual(self.apply_pending(), 0)
        # Use the same runnable filter as the central controller.
        ready = list(cli.pending_jobs(self.worker.db, self.cfg, {}, 1))
        self.assertEqual(len(ready), 1)
        self.job = ready[0][1]
        return self.run_controller()

    def snapshot_ci(self, state, conclusion):
        if not hasattr(self, "github_fixture"):
            self.github_fixture = github.GitHub()
            self.github_fixture.classic = {"checks": []}
        remote = self.github_fixture
        sha = self.prs[0]["head"]["sha"]
        slug = self.job["slug"]
        pr = self.prs[0] | {"id": 3, "draft": False, "html_url": f"https://github.com/{slug}/pull/3"}
        pr["base"] = pr["base"] | {"sha": self.base, "repo": {"id": 123, "full_name": slug}}
        remote.details[3] = pr
        remote.pulls = [[pr]]
        remote.checks[sha] = [github.check(11 if sha == self.old_head else 12, state, conclusion)]
        remote.runs[sha] = [github.run(80 if sha == self.old_head else 81, sha, 1, state, conclusion)]
        with patch("masf.queue.command", side_effect=lambda args: remote.command(
            [*args[:3], args[3].replace(slug, github.SLUG)]
        )):
            self.assertEqual(observe.observe(self.worker.db, queue_api, self.cfg, {slug: 123}), 0)


def create_fixture(test):
    class Fixture(DashboardFixture, factory.Delivery):
        pass

    fixture = Fixture()
    test.addCleanup(fixture.doCleanups)
    fixture.setUp()
    fixture.start_dashboard()
    return fixture


class DashboardAPI(unittest.TestCase):
    def setUp(self):
        self.fixture = create_fixture(self)

    def test_question_protected_files_and_resolved_block_continue(self):
        app = self.fixture
        app.scenario()
        self.assertIn('lang="es"', app.request("/dashboard/")[1])
        self.assertEqual(app.request(JOB + "/actions", app.payload("resolve"))[1]["error"]["code"],
                         "incompatible_state")
        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.data()["state"], "needs_input")
        question, = app.data()["intervention"]["questions"]
        self.assertIn("saludo", question["text"])
        self.assertEqual(app.context()["actions"][0]["kind"], "answer")

        data = app.payload("answer", evidence="Para Ana")
        path = JOB + "/actions"
        key = "stable-browser-request-key"
        self.assertEqual(app.request(path, data, key, token="incorrect")[0], 401)
        self.assertEqual(app.request(path, data, key, Origin="https://outside.invalid")[0], 403)
        self.assertTrue(queue.claim(app.worker.db, app.job, "other-operator", 60))
        self.assertEqual(app.request(path, data, key)[1]["error"]["code"], "decision_in_progress")
        with app.worker.db:
            app.worker.db.execute("DELETE FROM leases WHERE token='other-operator'")
        queue.save(app.worker.db, app.job, "needs_input", reason="Contexto renovado")
        self.assertEqual(app.request(path, data, key)[1]["error"]["code"], "revision_conflict")
        data = app.payload("answer", evidence="Para Ana")
        response = app.request(path, data, key)
        self.assertEqual(response[0], 202, response)
        self.assertEqual(app.request(path, data, key), response)
        accepted = response[1]["data"]
        self.assertEqual(app.receipt(accepted), accepted)
        self.assertEqual(app.data()["state"], "needs_input")
        self.assertEqual(app.request(JOB + "/actions/" + accepted["id"], token=None)[0], 401)
        self.assertEqual(app.request("/v1/jobs/123/2/actions/" + accepted["id"])[0], 404)
        self.assertEqual(app.request(path, data | {"evidence": "Otro"}, key)[1]["error"]["code"],
                         "idempotency_conflict")

        self.assertEqual(app.run_ready(), 1)
        applied = app.receipt(accepted)
        self.assertEqual((applied["status"], applied["state"]), ("applied", "planning"))
        self.assertEqual(app.request(path, data, key), (200, {"version": 1, "data": applied}))
        self.assertEqual(app.data()["state"], "blocked")
        offer, = app.context()["actions"]
        self.assertEqual(offer["paths"], [app.changed])
        self.assertEqual(app.request(path, app.payload("allow_paths", options={"paths": ["*"]}))[0], 422)
        self.assertEqual(app.request(path, app.payload("allow_paths", options={"paths": offer["paths"]},
                                                     confirm=False))[0], 422)
        self.assertEqual(app.submit("allow_paths", options={"paths": offer["paths"]})["resume_state"], "checking")
        self.assertEqual(app.run_ready(), 1)
        self.assertEqual(app.data()["checks"]["sha"], app.data()["candidate_sha"])

        app.deny = False
        self.assertEqual(app.submit("resolve")["resume_state"], "publishing")
        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.data()["state"], "pr_open")
        operations = [entry["decision"] for event in app.data(JOB + "/events") for entry in event["operations"]
                      if entry["decision"]["result"] == "applied"]
        self.assertEqual([(entry["kind"], entry["resume_state"]) for entry in operations],
                         [("answer", "planning"), ("allow_paths", "checking"), ("resolve", "publishing")])
        self.assertTrue(all(entry["operator"] == OPERATOR for entry in operations))
        self.assertEqual((operations[0]["question"], operations[0]["answer"], operations[0]["question_attempt"]),
                         (question["text"], "Para Ana", question["attempt_id"]))
        self.assertEqual(app.agent_calls[1][1]["operator_feedback"]["question_attempt"], question["attempt_id"])
        attempts = app.data(JOB + "/attempts")
        self.assertEqual([entry["model"]["effective"] for entry in attempts if entry["role"] == "plan"],
                         ["fixture-plan-1", "fixture-plan-2"])
        self.assertTrue(any(entry["model"]["availability"] == "unknown" for entry in attempts))
        self.assertEqual({entry["provider"] for entry in attempts if entry["provider"]}, {"codex", "cursor"})

    def test_cli_and_api_compete_for_same_revision(self):
        app = self.fixture
        app.scenario()
        app.run_ready()
        payload = app.payload("answer", evidence="Para Ana")
        entered, release = threading.Event(), threading.Event()
        path = JOB + "/actions"

        def lookup(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return app.api(*args, **kwargs)

        kwargs = dict(answer=True, evidence="Para Ana", revision=payload["revision"],
                      idempotency_key="competing-cli-request")
        target = f"{app.job['slug']}#1"
        with ThreadPoolExecutor() as threads, patch("masf.queue.api", lookup), patch("masf.cli.api", app.api):
            response = threads.submit(app.request, path, payload, "winning-api-request")
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ActionError) as busy:
                    cli.resume(app.worker.db, app.cfg, target, **kwargs)
                self.assertEqual(busy.exception.code, "decision_in_progress")
            finally:
                release.set()
            status, body = response.result()
            self.assertEqual(status, 202, body)
            with self.assertRaises(ActionError) as pending:
                cli.resume(app.worker.db, app.cfg, target, **kwargs)
            self.assertEqual(pending.exception.code, "decision_in_progress")
        self.assertEqual(app.run_ready(), 1)
        with self.assertRaises(ActionError) as stale:
            cli.resume(app.worker.db, app.cfg, target, **kwargs)
        self.assertEqual(stale.exception.code, "revision_conflict")
        operations = [op["decision"] for event in app.data(JOB + "/events") for op in event["operations"]]
        self.assertEqual([(op["origin"], op["result"], op["id"]) for op in operations],
                         [("api", result, body["data"]["id"]) for result in ("accepted", "applied")])
        self.assertEqual(app.plan_calls, 2)
        self.assertEqual(list(cli.pending_jobs(app.worker.db, app.cfg, {}, 1)), [])

    def test_decision_survives_process_loss_at_commit_boundary(self):
        app = self.fixture
        app.scenario()
        app.run_ready()
        payload = app.payload("answer", evidence="Para Ana")
        path = JOB + "/actions"
        key = "restart-same-decision"
        script = """
import json, os, signal, sys
from pathlib import Path
from unittest.mock import patch
from masf import cli, queue
from masf.config import load

root, config, target, operator, key, issue, payload, boundary = sys.argv[1:]
payload = json.loads(payload)
save = queue.save
def terminate(*args, **kwargs):
    if boundary == "after":
        save(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
with (queue.connect(Path(root)) as db,
      patch.object(cli, "operator_name", return_value=operator),
      patch.object(cli, "api", return_value=json.loads(issue)),
      patch.object(queue, "write_event" if boundary == "before" else "save", terminate)):
    cli.resume(db, load(Path(config)), target, answer=True, evidence=payload["evidence"],
               revision=payload["revision"], idempotency_key=key)
"""

        def interrupted(boundary):
            process = subprocess.run(
                [sys.executable, "-c", script, str(app.root / "state"), str(app.server.config),
                 f"{app.job['slug']}#1", OPERATOR, key, json.dumps(app.issue), json.dumps(payload), boundary],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr)

        interrupted("before")
        self.assertEqual(app.data()["revision"], payload["revision"])
        self.assertEqual(app.request(path, payload, key)[1]["error"]["code"], "decision_in_progress")
        with app.worker.db:
            app.worker.db.execute("UPDATE leases SET expires=0")
        interrupted("after")
        response = app.request(path, payload, key)
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]["data"]["origin"], "cli")
        self.assertEqual(app.request(path, payload, key), response)
        self.assertEqual(app.run_ready(), 1)
        self.assertEqual(app.plan_calls, 2)
        self.assertEqual(len([op for event in app.data(JOB + "/events") for op in event["operations"]]), 1)
        self.assertEqual(app.worker.db.execute("SELECT count(*) FROM action_receipts").fetchone()[0], 1)
        self.assertEqual(list(cli.pending_jobs(app.worker.db, app.cfg, {}, 1)), [])
        legacy = {name: value for name, value in response[1]["data"].items() if name not in {"repo_id", "number"}}
        with app.worker.db:
            app.worker.db.execute("UPDATE action_receipts SET data=? WHERE key=?", (json.dumps(legacy), key))
        self.assertEqual(app.receipt(legacy), legacy)
        self.assertEqual(app.request("/v1/jobs/123/2/actions/" + legacy["id"])[0], 404)

    def test_http_acceptance_survives_controller_restart_and_dispatches_once(self):
        app = self.fixture
        app.scenario()
        self.assertEqual(app.run_ready(), 0)
        path = JOB + "/actions"
        payload = app.payload("answer", evidence="Para Ana")
        key = "restart-after-http-acceptance"
        status, body = app.request(path, payload, key)
        self.assertEqual(status, 202, body)
        accepted = body["data"]
        self.assertEqual(app.context()["pending_receipt"], accepted)
        self.assertEqual(app.data()["revision"], payload["revision"])
        app.worker.db.close()
        root = app.root / "state"
        before_apply = """
import os, signal, sys
from pathlib import Path
from unittest.mock import patch
from masf import cli, queue
from masf.config import load

root, config = map(Path, sys.argv[1:])
with (queue.connect(root) as db,
      patch.object(cli, "process_pending", side_effect=lambda *a, **kw:
                   os.kill(os.getpid(), signal.SIGKILL))):
    cli.tick(db, load(config), config, root, True)
"""
        interrupted = subprocess.run([sys.executable, "-c", before_apply, str(root), str(app.server.config)],
                                     capture_output=True, text=True, timeout=10)
        self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stderr)
        self.assertEqual(app.receipt(accepted)["status"], "accepted")

        def create(*args):
            instance = worker.Worker(*args)
            instance.url = str(app.source)
            instance.recover = lambda: None
            instance.test = app.check
            return instance

        with (closing(queue.connect(root)) as restarted,
              patch.multiple(cli, api=app.api, Worker=create, scan=lambda *_, **__: 0,
                             observe=lambda *_: 0, poll_ci_jobs=lambda *_: 0)):
            self.assertEqual(app.request(path, payload, key), (status, body))
            self.assertEqual(cli.tick(restarted, app.cfg, app.server.config, root, True), 1)
            self.assertEqual(cli.tick(restarted, app.cfg, app.server.config, root, True), 0)
            self.assertEqual(restarted.execute("SELECT count(*) FROM action_receipts").fetchone()[0], 1)
            self.assertEqual(restarted.execute("SELECT count(*) FROM pending_actions").fetchone()[0], 0)
        self.assertEqual(app.plan_calls, 2)
        self.assertEqual(app.data()["state"], "blocked")
        self.assertEqual(app.receipt(accepted)["status"], "applied")
        operations = [op["decision"] for event in app.data(JOB + "/events") for op in event["operations"]]
        self.assertEqual([(op["id"], op["result"]) for op in operations],
                         [(accepted["id"], "accepted"), (accepted["id"], "applied")])

    def test_pending_scope_change_rejects_and_filters_feedback_and_errors(self):
        app = self.fixture
        app.scenario()
        app.run_ready()
        headers = {"Sec-Fetch-Site": "same-site"}
        self.assertEqual(app.request(JOB + "/actions", app.payload("answer"), **headers)[0], 403)
        auth = app.root / "actions.json"
        auth.write_text(json.dumps({"operator": OPERATOR, "token": TOKEN, "permissions": []}))
        with patch.object(app.server, "actions", Actions(app.server.root, app.server.config, auth)):
            status, body = app.request(JOB + "/actions", app.payload("answer"))
            self.assertEqual((status, body["error"]["code"]), (403, "insufficient_permissions"))
            self.assertEqual(app.request(JOB, token=None)[0], 200)
        evidence = f"Respuesta: {TOKEN} password=fixture-private-secret"
        logs = io.StringIO()
        with redirect_stderr(logs), redirect_stdout(logs):
            with patch.object(worker, "api", side_effect=ProcessError(evidence)):
                status, unavailable = app.request(JOB + "/actions", app.payload("answer", evidence=evidence))
                self.assertEqual((status, unavailable["error"]["code"]), (503, "unavailable"))
            receipt = app.submit("answer", evidence=evidence)
            with patch.object(worker, "api", side_effect=ProcessError(evidence)):
                self.assertEqual(app.apply_pending(), 1)
            self.assertEqual(app.receipt(receipt)["status"], "accepted")
            app.issue["body"] = "Alcance actualizado después de la aceptación"
            self.assertEqual(app.apply_pending(), 0)
        rejected = app.receipt(receipt)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["error"]["code"], "revision_conflict")
        self.assertEqual(app.data()["state"], "needs_input")
        self.assertEqual(app.context()["actions"][0]["kind"], "confirm_scope")
        self.assertEqual(list(cli.pending_jobs(app.worker.db, app.cfg, {}, 1)), [])
        events = app.data(JOB + "/events")
        entries = [op["decision"] for event in events for op in event["operations"]]
        self.assertEqual([(op["id"], op["result"]) for op in entries],
                         [(receipt["id"], "accepted"), (receipt["id"], "rejected")])
        self.assertIn("[redacted]", entries[0]["evidence"])
        for output in (json.dumps([receipt, rejected, events, unavailable]), logs.getvalue()):
            self.assertNotIn(TOKEN, output)
            self.assertNotIn("fixture-private-secret", output)

    def test_timeout_resumes_compatible_history_on_same_round(self):
        app = self.fixture
        app.cfg["service"]["max_recovery_attempts"] = 0
        copies = []

        def interrupted(role, job, cfg, workspace, inputs, owner, **kwargs):
            if role == "build":
                copies.append(workspace)
                if len(copies) == 1:
                    (workspace / "partial").write_text("retained plan and files")
                    raise CommandTimeout("El proceso de construcción excedió el plazo")
                self.assertEqual(cfg["service"]["step_timeout_seconds"], 1200)
                self.assertEqual((workspace / "partial").read_text(), "retained plan and files")
                self.assertEqual(inputs["plan"]["criteria"], ["AC1 greet"])
            return app.agent(role, job, cfg, workspace, inputs, owner, **kwargs)

        with patch("masf.providers.invoke", interrupted):
            self.assertEqual(app.run_ready(), 1)
            old = app.job["build_workspace"]
            current = app.worker.checkout(app.base)
            foreign = app.root / "jobs" / "123-2" / "copy"
            foreign.mkdir(parents=True)
            wrong_scope = app.worker.checkout(app.base)
            for path, fingerprint in ((foreign, app.job["fingerprint"]), (wrong_scope, "scope-invalid")):
                queue.save(app.worker.db, app.job, "blocked", build_workspace=old | {
                    "path": str(path), "fingerprint": fingerprint,
                })
            queue.save(app.worker.db, app.job, "blocked", build_workspace=old | {"path": str(current)})
            offer, = app.context()["actions"]
            self.assertEqual(offer["kind"], "extend_timeout")
            self.assertEqual(offer["previous_seconds"], 900)
            self.assertEqual(app.context()["build_timeout"], {
                "stage": "build", "seconds": 900, "rounds": 1, "round_limit": app.cfg["service"]["max_build_rounds"],
            })
            self.assertEqual(len(offer["copies"]), 2)
            historical, = [copy for copy in offer["copies"] if not copy["current"]]
            path = JOB + "/actions"
            for invalid_copy in (foreign, wrong_scope):
                identifier = digest([123, 1, str(invalid_copy), old["failed_attempt"]])
                bad = app.payload("extend_timeout", options={"seconds": 1200, "reference": identifier})
                self.assertEqual(app.request(path, bad)[0], 422)
            for seconds in (900, 86401, 1200.5):
                invalid_timeout = app.payload("extend_timeout", options={
                    "seconds": seconds, "reference": historical["reference"],
                })
                self.assertEqual(app.request(path, invalid_timeout)[0], 422)
            app.submit("extend_timeout", options={"seconds": 1200, "reference": historical["reference"]})
            self.assertEqual(app.run_ready(), 0)
        self.assertEqual(copies, [copies[0], copies[0]])
        self.assertEqual(app.data()["limits"]["rounds"], 1)
        self.assertEqual(app.data()["state"], "pr_open")

    def test_scope_confirmation_and_role_reassignment(self):
        app = self.fixture
        app.issue["body"] = "Alcance nuevo"
        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.context()["actions"][0]["kind"], "confirm_scope")
        app.issue["body"] = "Alcance más reciente"
        path = JOB + "/actions"
        self.assertEqual(app.request(path, app.payload("confirm_scope"))[1]["error"]["code"], "revision_conflict")
        self.assertEqual(app.submit("confirm_scope")["resume_state"], "planning")
        with patch("masf.providers.invoke", side_effect=ProcessError("HTTP 401 Unauthorized")):
            self.assertEqual(app.run_ready(), 1)
        offer, = [item for item in app.context()["actions"] if item["kind"] == "assign_provider"]
        self.assertEqual((offer["role"], offer["providers"]), ("plan", ["cursor"]))
        disabled = app.payload("assign_provider", options={"role": "plan", "provider": "claude"})
        self.assertEqual(app.request(path, disabled)[0], 422)
        self.assertEqual(app.submit("assign_provider", options={"role": "plan", "provider": "cursor"})["resume_state"], "planning")
        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.data(JOB + "/attempts")[1]["provider"], "cursor")

    def test_expanding_exhausted_rounds_applies_to_unpublished_work(self):
        app = self.fixture
        app.cfg["service"]["max_build_rounds"] = 2
        app.cfg["service"]["max_recovery_attempts"] = 0
        queue.save(app.worker.db, app.job, "blocked", rounds=2, retry="building",
                   reason="Rondas de construcción agotadas", plan={"criteria": ["AC1 greet"]})
        offer, = [item for item in app.context()["actions"] if item["kind"] == "add_rounds"]
        self.assertEqual(offer["current_limit"], 2)
        for extra in (True, 0, 26):
            self.assertEqual(app.request(JOB + "/actions", app.payload("add_rounds", options={"extra": extra}))[0], 422)
        self.assertEqual(app.submit("add_rounds", options={"extra": 1})["resume_state"], "building")
        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.data()["limits"]["build_limit"], 3)

    def test_resolved_commit_requires_work_scope_and_current_base(self):
        app = self.fixture
        app.open_conflicting_pr()
        app.cfg["service"]["max_recovery_attempts"] = 0
        self.assertEqual(app.ci_tick(), 1)
        blocked = app.ci_job()
        self.assertEqual(app.data()["base_conflict"]["paths"], ["README"])
        tree = app.worker.git("rev-parse", app.old_head + "^{tree}")
        resolved = app.worker.git("commit-tree", tree, "-p", app.old_head, "-p", app.base, "-m", "resolved")
        path = JOB + "/actions"
        body = app.payload("resolved_commit", options={"sha": resolved})
        self.assertEqual(app.request(path, body)[0], 422)
        app.keep_resolution(resolved, job=blocked | {"fingerprint": "wrong-scope"})
        self.assertEqual(app.request(path, body)[0], 422)
        app.keep_resolution(resolved, job=blocked)
        offer, = [item for item in app.context()["actions"] if item["kind"] == "resolved_commit"]
        self.assertEqual(offer["commits"], [resolved])

        previous_base = app.base
        app.advance_open_pr(message="newer base")
        self.assertEqual(app.request(path, body)[1]["error"]["code"], "revision_conflict")
        self.assertEqual(app.data()["observed_base"], app.base)
        self.assertEqual(app.data()["candidate_sha"], app.old_head)
        self.assertNotIn("resolved_commit", [item["kind"] for item in app.context()["actions"]])

        # Restore the recorded observation to exercise acceptance on the current base.
        app.base = previous_base
        app.prs[0]["base"]["sha"] = previous_base
        factory.command(["git", "update-ref", "refs/heads/main", previous_base], cwd=app.source)
        queue.save(app.worker.db, blocked, "blocked", base_conflict=blocked["base_conflict"])
        data = app.payload("resolved_commit", options={"sha": resolved})
        key = "repeat-resolved-commit"
        receipt = app.request(path, data, key)
        self.assertEqual(receipt[0], 202, receipt)
        self.assertEqual(app.request(path, data, key), receipt)
        self.assertEqual(app.apply_pending(), 0)
        applied = app.ci_job()
        self.assertEqual((applied["state"], applied["candidate"]), ("checking", resolved))
        self.assertEqual((applied["base_checked"], applied["checks"], applied["review"]), (None, {}, {}))
        self.assertEqual(app.ci_tick(), 0)
        finished = app.ci_job()
        self.assertEqual(finished["published_head"], resolved)
        self.assertEqual((finished["base_checked"], finished["checks"]["sha"], finished["review"]["sha"]),
                         (previous_base, resolved, resolved))
        self.assertEqual(len(app.prs), 1)

    def test_expand_exhausted_recovery_budget_and_continue_after_reopen(self):
        app = self.fixture
        app.cfg["service"]["max_recovery_attempts"] = 1
        config = app.server.config
        config.write_text(config.read_text().replace("max_recovery_attempts = 2", "max_recovery_attempts = 1"))

        def interrupted(role, *args, **kwargs):
            if role == "review":
                raise recovery.RecoverableFailure("Revisión temporalmente interrumpida")
            return app.agent(role, *args, **kwargs)

        with patch("masf.providers.invoke", interrupted):
            self.assertEqual(app.run_ready(), 1)
            waiting = app.data()
            self.assertEqual(waiting["state"], "retry_wait")
            self.assertFalse(waiting["intervention"]["pending"])
            self.assertEqual(waiting["recovery"]["used"], 1)
            self.assertIsNotNone(waiting["recovery"]["next_at"])
            self.assertEqual(app.context()["actions"], [])
            with patch("masf.cli.time.time", return_value=waiting["recovery"]["next_at"] + 1):
                self.assertEqual(app.run_ready(), 1)
            blocked = app.data()
            self.assertEqual(blocked["state"], "blocked")
            self.assertTrue(blocked["intervention"]["pending"])
            self.assertEqual((blocked["recovery"]["used"], blocked["recovery"]["limit"]), (1, 1))
            self.assertEqual(blocked["recovery"]["history"][0]["action"], "retry")
            response = app.submit("add_recoveries", options={"extra": 1})
            self.assertEqual(response["resume_state"], "reviewing")
            with closing(queue.connect(app.root / "state")) as reopened:
                self.assertEqual(app.apply_pending(reopened), 0)
                job = json.loads(reopened.execute("SELECT data FROM jobs").fetchone()[0])
                self.assertEqual(recovery.limit(job, app.cfg), 2)
                self.assertEqual(len(job["recoveries"]), 2)
                self.assertEqual(job["recoveries"][0]["reason"], blocked["recovery"]["history"][0]["reason"])
                self.assertEqual(job["decisions"][-1]["expected_revision"], blocked["revision"])
                ready, = cli.pending_jobs(reopened, app.cfg, {}, 1)
                self.assertEqual(ready[1]["candidate"], blocked["candidate_sha"])
            self.assertEqual(app.data()["recovery"]["used"], 2)

        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.data()["state"], "pr_open")
        self.assertEqual(app.data()["candidate_sha"], blocked["candidate_sha"])
        self.assertEqual(app.roles.count("build"), 1)

    def test_ci_intervention_and_repair_preserve_history_at_both_shas(self):
        app = self.fixture
        app.open_ci_pr()
        app.snapshot_ci("in_progress", None)
        pull = "/v1/pulls/123/3"
        self.assertEqual(app.data(pull)["ci"]["checks"]["check:11"]["phase"], "in_progress")
        app.annotation = app.ci_logs = "Runner lost"
        self.assertEqual(app.ci_tick(observe_only=True), 0)
        self.assertEqual(app.data()["repair"]["diagnostics"][0]["kind"], "infrastructure")
        with patch("masf.cli.time.time", return_value=app.ci_job()["ci_retry_at"] + 1):
            self.assertEqual(app.ci_tick(), 0)
        self.assertTrue(app.data()["intervention"]["ci"])
        self.assertEqual(app.submit("resolve")["resume_state"], "pr_open")
        self.assertEqual(app.apply_pending(), 0)

        app.action_attempt = 2
        app.annotation = app.ci_logs = "FAIL: test_ci_repair"
        app.snapshot_ci("completed", "failure")
        self.assertEqual(app.ci_tick(observe_only=True), 0)
        self.assertEqual(app.data()["state"], "building")
        self.assertEqual(app.data()["repair"]["diagnostics"][0]["kind"], "functional")
        self.assertEqual(app.ci_tick(), 0)
        self.assertEqual(app.ci_tick(), 0)
        app.snapshot_ci("completed", "success")
        snapshots = {snapshot["sha"]: snapshot for snapshot in app.data(pull + "/ci")}
        head = app.data(pull)["head"]["sha"]
        self.assertNotEqual(head, app.old_head)
        self.assertEqual(snapshots[head]["checks"]["check:12"]["conclusion"], "success")
        self.assertEqual(snapshots[app.old_head]["checks"]["check:11"]["conclusion"], "failure")


if __name__ == "__main__":
    unittest.main()
