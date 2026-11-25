"""Dashboard acceptance against loopback HTTP and the controller's temporary queue."""

import http.client
import json
import os
import threading
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

from masf import cli, observe, queue, worker
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
        self.assertEqual(status, 200, result)
        return result["data"]

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
        self.assertEqual(response[0], 200, response)
        self.assertEqual(app.request(path, data, key), response)
        self.assertEqual(app.request(path, data | {"evidence": "Otro"}, key)[1]["error"]["code"],
                         "idempotency_conflict")

        self.assertEqual(app.run_ready(), 1)
        self.assertEqual(app.data()["state"], "blocked")
        offer, = app.context()["actions"]
        self.assertEqual(offer["paths"], [app.changed])
        self.assertEqual(app.request(path, app.payload("allow_paths", options={"paths": ["*"]}))[0], 422)
        self.assertEqual(app.request(path, app.payload("allow_paths", options={"paths": offer["paths"]},
                                                     confirm=False))[0], 422)
        self.assertEqual(app.submit("allow_paths", options={"paths": offer["paths"]})["state"], "checking")
        self.assertEqual(app.run_ready(), 1)
        self.assertEqual(app.data()["checks"]["sha"], app.data()["candidate_sha"])

        app.deny = False
        self.assertEqual(app.submit("resolve")["state"], "publishing")
        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.data()["state"], "pr_open")
        operations = [entry["decision"] for event in app.data(JOB + "/events") for entry in event["operations"]]
        self.assertEqual([(entry["kind"], entry["resume_state"]) for entry in operations],
                         [("answer", "planning"), ("allow_paths", "checking"), ("resolve", "publishing")])
        self.assertTrue(all(entry["operator"] == OPERATOR for entry in operations))
        attempts = app.data(JOB + "/attempts")
        self.assertEqual([entry["model"]["effective"] for entry in attempts if entry["role"] == "plan"],
                         ["fixture-plan-1", "fixture-plan-2"])
        self.assertTrue(any(entry["model"]["availability"] == "unknown" for entry in attempts))
        self.assertEqual({entry["provider"] for entry in attempts if entry["provider"]}, {"codex", "cursor"})

    def test_timeout_resumes_compatible_history_on_same_round(self):
        app = self.fixture
        app.cfg["service"]["max_recovery_attempts"] = 0
        copies = []

        def interrupted(role, job, cfg, workspace, inputs, owner, **kwargs):
            if role == "build":
                copies.append(workspace)
                if len(copies) == 1:
                    (workspace / "partial").write_text("retained plan and files")
                    raise CommandTimeout("docker: tiempo agotado (900s)")
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
            self.assertEqual(len(offer["copies"]), 2)
            historical, = [copy for copy in offer["copies"] if not copy["current"]]
            path = JOB + "/actions"
            for invalid_copy in (foreign, wrong_scope):
                identifier = digest([123, 1, str(invalid_copy), old["failed_attempt"]])
                bad = app.payload("extend_timeout", options={"seconds": 1200, "reference": identifier})
                self.assertEqual(app.request(path, bad)[0], 422)
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
        self.assertEqual(app.submit("confirm_scope")["state"], "planning")
        with patch("masf.providers.invoke", side_effect=ProcessError("HTTP 401 Unauthorized")):
            self.assertEqual(app.run_ready(), 1)
        offer, = [item for item in app.context()["actions"] if item["kind"] == "assign_provider"]
        self.assertEqual((offer["role"], offer["providers"]), ("plan", ["cursor"]))
        self.assertEqual(app.submit("assign_provider", options={"provider": "cursor"})["state"], "planning")
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
        self.assertEqual(app.submit("add_rounds", options={"extra": 1})["state"], "building")
        self.assertEqual(app.run_ready(), 0)
        self.assertEqual(app.data()["limits"]["build_limit"], 3)

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
        self.assertEqual(app.submit("resolve")["state"], "pr_open")

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
