import copy
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from masf import cli, queue
from masf.system import ProcessError


SLUG = "yuloma/masf"
REPO = f"repos/{SLUG}"
CANDIDATE = "bb747e16b42c2aaf05ac9275c9625cde90a49aec"
PILOT_HEAD = "d8cda48a51ee46eb285ac866e8b58d4ad89415db"
BASE = "a" * 40
NEW_HEAD = "b" * 40


def pull(number=23, sha=PILOT_HEAD, *, state="open", merged=False):
    repository = {"id": 7, "full_name": SLUG}
    return {
        "id": 900 + number, "number": number, "state": state, "draft": True,
        "html_url": f"https://github.com/{SLUG}/pull/{number}", "body": "",
        "merged_at": "2026-11-24T13:00:00Z" if merged else None,
        "head": {"repo": repository, "ref": "masf/issue-10", "sha": sha},
        "base": {"repo": repository, "ref": "main", "sha": BASE},
    }


def check(ident, status, conclusion=None, name="unit", suite=4):
    return {
        "id": ident, "name": name, "status": status, "conclusion": conclusion,
        "check_suite": {"id": suite}, "app": {"id": 42},
        "html_url": f"https://github.com/{SLUG}/checks/{ident}",
        "output": {"title": "Checks", "annotations_count": 1},
    }


def run(ident, sha, attempt, status, conclusion=None):
    return {
        "id": ident, "workflow_id": 3, "check_suite_id": ident - 76, "head_sha": sha,
        "run_attempt": attempt, "status": status, "conclusion": conclusion,
        "name": "Factory", "html_url": f"https://github.com/{SLUG}/actions/runs/{ident}",
    }


def job(ident, check_number, sha, attempt, status, conclusion=None):
    return {
        "id": ident, "name": "unit", "head_sha": sha, "run_attempt": attempt,
        "status": status, "conclusion": conclusion,
        "check_run_url": f"https://api.github.com/{REPO}/check-runs/{check_number}",
        "html_url": f"https://github.com/{SLUG}/actions/runs/80/job/{ident}",
    }


class GitHub:
    def __init__(self):
        self.pulls = [[pull()]]
        self.details = {}
        self.issue_details = {}
        self.checks = {}
        self.statuses = {}
        self.runs = {}
        self.jobs = {}
        self.attempts = {}
        self.requests = []
        self.failures = {}
        self.issues = []
        self.classic = {"checks": [{"context": "unit", "app_id": 42}]}
        self.rules = []

    def command(self, args):
        if args[:3] != ["gh", "api", "--include"]:
            raise AssertionError(args)
        url = urlsplit(args[3])
        endpoint, params = url.path, parse_qs(url.query)
        page = int(params.get("page", [1])[0])
        self.requests.append((endpoint, params))
        error = self.failures.get((endpoint, page))
        if error:
            raise error
        headers = ""
        if endpoint == REPO:
            result = {"id": 7, "default_branch": "main"}
        elif endpoint == f"{REPO}/issues":
            result = self.issues
        elif endpoint.startswith(f"{REPO}/issues/"):
            result = self.issue_details[int(endpoint.rsplit("/", 1)[-1])]
        elif endpoint == f"{REPO}/pulls":
            result = self.pulls[page - 1] if page <= len(self.pulls) else []
            if page < len(self.pulls):
                headers = f'Link: <https://api.github.com/{endpoint}?page={page + 1}>; rel="next"\n'
        elif endpoint.startswith(f"{REPO}/pulls/"):
            result = self.details[int(endpoint.rsplit("/", 1)[-1])]
        elif endpoint == f"{REPO}/branches/main":
            result = {"protected": True}
        elif endpoint == f"{REPO}/branches/main/protection/required_status_checks":
            result = self.classic
        elif endpoint == f"{REPO}/rules/branches/main":
            result = self.rules[(page - 1) * 100:page * 100]
        elif endpoint == f"{REPO}/actions/runs":
            result = {"workflow_runs": self.runs.get(params["head_sha"][0], [])}
        elif endpoint.startswith(f"{REPO}/actions/runs/") and endpoint.endswith("/jobs"):
            rid, _, attempt, _ = endpoint.split("/")[-4:]
            result = {"jobs": self.jobs.get((int(rid), int(attempt)), [])}
        elif endpoint.startswith(f"{REPO}/actions/runs/"):
            rid, _, attempt = endpoint.split("/")[-3:]
            result = self.attempts[(int(rid), int(attempt))]
        elif endpoint.startswith(f"{REPO}/actions/jobs/"):
            return (
                'HTTP/2 200 OK\n\nordinary context\nERROR password="a private value"\n'
                "fatal ghp_\x1b[31mfixtureToken\x1b[0m\nerror opaque-fixture\n::add-mask::opaque-fixture"
            )
        elif endpoint.startswith(f"{REPO}/check-runs/"):
            result = (
                [{"path": "test.py", "start_line": 8, "end_line": 8, "message": "failure api_key=sk-fixture"}]
                if endpoint.endswith("/annotations")
                else {"output": {"summary": "broken secret=privatefixture"}}
            )
        elif endpoint.startswith(f"{REPO}/commits/"):
            sha = endpoint.split("/")[4]
            if endpoint.endswith("/check-runs"):
                items = self.checks.get(sha, [])
                result = {"check_runs": items[(page - 1) * 100:page * 100], "total_count": len(items)}
            elif endpoint.endswith("/statuses"):
                result = self.statuses.get(sha, [])[(page - 1) * 100:page * 100]
            else:
                parents = [CANDIDATE, BASE] if sha == PILOT_HEAD else []
                result = {"sha": BASE if sha == "main" else sha, "parents": [{"sha": value} for value in parents]}
        else:
            raise AssertionError(endpoint)
        return f"HTTP/2 200 OK\n{headers}\n{json.dumps(result)}"


class Observation(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = queue.connect(self.root)
        self.addCleanup(lambda: self.db.close())
        self.config = self.root / "factory.toml"
        self.cfg = {
            "repositories": [
                {"slug": SLUG, "enabled": True, "label": "masf:ready"},
                {"slug": "disabled/repo", "enabled": False},
            ],
            "service": {"max_parallel_jobs": 1, "poll_seconds": 5},
        }
        self.github = GitHub()
        mock = patch("masf.queue.command", side_effect=self.github.command)
        mock.start()
        self.addCleanup(mock.stop)
        self.addCleanup(setattr, queue, "_retry_at", 0)

    def insert_job(self, number=10, state="pr_open", pr_number=23, **fields):
        job_data = {
            "repo_id": 7, "slug": SLUG, "number": number, "state": state,
            "candidate": CANDIDATE, "rounds": 3, "pr_number": pr_number,
            "branch": "main",
        } | fields
        with self.db:
            self.db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?)", (7, number, SLUG, state, json.dumps(job_data)),
            )

    def poll(self):
        with (
            patch("masf.cli.process_task", return_value=0),
            patch("masf.cli.poll_ci_jobs", return_value=0),
        ):
            return cli.tick(self.db, self.cfg, self.config, self.root, True)

    def output(self, *args):
        out = io.StringIO()
        with (
            patch("masf.cli.load", return_value=self.cfg),
            patch("masf.cli.xdg", return_value=self.root),
            patch("sys.argv", ["masf", *args]),
            redirect_stdout(out),
        ):
            self.assertEqual(cli.main(), 0)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def status(self, number=23):
        return self.output("status", "--pr", f"{SLUG}#{number}")[0]

    def events(self, sha=None):
        args = ["logs", "--pr", f"{SLUG}#23"]
        if sha:
            args.extend(["--sha", sha])
        return self.output(*args)

    def test_lists_paginated_enabled_prs_and_requires_verified_publication_marker(self):
        self.insert_job(state="publishing", pr_number=None, owner="localmarker")
        published = pull(23, CANDIDATE)
        published["body"] = "<!-- masf:localmarker -->"
        unlinked = pull(24, CANDIDATE)
        unlinked["body"] = published["body"]
        unlinked["head"] = unlinked["head"] | {"repo": {"id": 8, "full_name": "fork/masf"}}
        self.github.pulls = [[published], [unlinked]]
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.status()["job"]["method"], "publication")
        self.assertIsNone(self.status(24)["job"])
        self.assertEqual(self.status(24)["head"]["repo_id"], 8)
        self.assertIn((f"{REPO}/pulls", {"state": ["open"], "per_page": ["100"], "page": ["2"]}), self.github.requests)
        self.assertFalse(any("disabled" in path for path, params in self.github.requests))

    def test_reads_all_check_runs_and_commit_statuses_with_required_rules(self):
        self.github.checks[PILOT_HEAD] = [check(i, "queued") for i in range(1, 102)]
        self.github.statuses[PILOT_HEAD] = [
            {"id": i, "context": "legacy", "state": "pending" if i == 201 else "success"}
            for i in range(201, 100, -1)
        ]
        self.github.rules = [{"type": "deletion"}] * 100 + [{
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "lint", "integration_id": 42}]},
        }]
        self.assertEqual(self.poll(), 0)
        ci = self.status()["ci"]
        self.assertEqual(len(ci["checks"]), 202)
        self.assertEqual(ci["checks"]["check:101"]["phase"], "queued")
        self.assertEqual(ci["checks"]["status:201"]["phase"], "pending")
        self.assertFalse(ci["checks"]["status:101"]["current"])
        self.assertEqual(ci["required"]["missing"], [{"name": "lint", "app_id": 42}])
        self.assertTrue(ci["required"]["known"])

    def test_operator_can_link_unassociated_observation_without_changing_job_state(self):
        self.insert_job(state="blocked", pr_number=None)
        self.assertEqual(self.poll(), 0)
        self.assertIsNone(self.status()["job"])
        args = ("link-pr", f"{SLUG}#23", f"{SLUG}#10")
        request_count = len(self.github.requests)
        self.assertEqual(self.output(*args), [])
        self.assertEqual(self.output(*args), [])
        self.assertEqual(len(self.github.requests), request_count)
        self.assertEqual(self.status()["job"]["method"], "operator")
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.status()["job"]["number"], 10)
        self.assertEqual(self.db.execute("SELECT state FROM jobs").fetchone()[0], "blocked")
        self.assertEqual(sum(entry.get("kind") == "pr_job" for entry in self.events()), 1)

    def test_combined_poll_records_ci_and_dispatches_repair_with_retry_delay(self):
        issue = {
            "number": 10, "state": "open", "labels": [{"name": "masf:ready"}],
            "title": "Repair CI", "body": "Keep the existing PR",
        }
        self.cfg["service"]["max_build_rounds"] = 5
        self.cfg["repositories"][0]["routing"] = {"build": "fake"}
        self.github.issue_details[10] = issue
        self.github.details[23] = pull(sha=CANDIDATE)
        self.github.pulls = [[self.github.details[23]]]
        self.github.checks[CANDIDATE] = [
            check(11, "completed", "failure") | {"head_sha": CANDIDATE},
        ]
        self.insert_job(
            linked_pr=23, repo=self.cfg["repositories"][0],
            fingerprint=queue.fingerprint(issue), plan={"summary": "Fix the failing check"},
        )
        original_command = self.github.command

        def command(args):
            result = original_command(args)
            if "/annotations?" in args[3]:
                return result.replace("failure api_key", "FAIL: test_ci_repair api_key")
            return result

        with (
            patch("masf.queue.command", side_effect=command),
            patch("masf.cli.process_task", return_value=0) as process,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.tick(self.db, self.cfg, self.config, self.root, True), 0)
            process.assert_called_once()
            repaired = process.call_args.args[-1]
            self.assertEqual(repaired["state"], "building")
            self.assertEqual(repaired["ci"]["sha"], CANDIDATE)
            self.assertEqual(repaired["ci_repair"]["diagnostics"][0]["kind"], "functional")
            self.assertEqual(self.status()["ci"]["checks"]["check:11"]["conclusion"], "failure")
            handled = repaired["ci_handled"]
            queue.save(self.db, repaired, "checking", ci_retry_at=cli.time.time() + 3600)
            process.reset_mock()
            self.assertEqual(cli.tick(self.db, self.cfg, self.config, self.root, True), 0)
            process.assert_not_called()
            persisted = json.loads(self.db.execute("SELECT data FROM jobs").fetchone()[0])
            self.assertEqual(persisted["ci_handled"], handled)
            self.assertEqual(persisted["rounds"], 3)

    def test_cli_keeps_repair_link_and_json_job_status(self):
        with patch("masf.cli.link_pr") as link:
            self.assertEqual(self.output(
                "link-pr", f"{SLUG}#10", "--pr", "23", "--head", CANDIDATE,
                "--evidence", "Reviewed scope",
            ), [])
            link.assert_called_once()
            self.assertEqual(link.call_args.args[2:], (
                f"{SLUG}#10", 23, CANDIDATE, "Reviewed scope",
            ))
        self.cfg["service"]["max_build_rounds"] = 5
        self.insert_job()
        self.assertEqual(self.poll(), 0)
        rows = self.output("status", "--json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["number"], 10)
        self.assertEqual(rows[0]["limits"]["build_limit"], 5)

    def test_pilot_keeps_candidate_and_rounds_during_serve_worker_and_records_merge(self):
        self.insert_job()
        self.github.issues = [{
            "number": 12, "state": "open", "labels": [{"name": "masf:ready"}],
            "title": "Task", "body": "Simulated issue",
        }]
        self.github.checks[PILOT_HEAD] = [check(11, "queued")]
        started, release = threading.Event(), threading.Event()
        delays = []

        def process(cfg, config, root, entry):
            started.set()
            release.wait(5)
            return 0

        def sleeper(delay):
            delays.append(delay)
            if len(delays) == 1:
                self.assertTrue(started.wait(2))
                self.github.checks[PILOT_HEAD] = [check(11, "in_progress")]
                return
            self.assertFalse(release.is_set())
            release.set()
            raise KeyboardInterrupt

        with (
            patch("masf.cli.load", return_value=self.cfg),
            patch("masf.cli.xdg", return_value=self.root),
            patch("masf.cli.process_task", side_effect=process) as worker,
            patch("masf.cli.poll_ci_jobs", return_value=0),
            patch("masf.cli.time", wraps=cli.time) as clock,
            patch("sys.argv", ["masf", "serve"]),
            redirect_stdout(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            clock.sleep.side_effect = sleeper
            cli.main()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(len(delays), 2)
        self.assertTrue(all(0 < delay <= 5 for delay in delays))
        observed = self.status()
        self.assertEqual(observed["head"]["sha"], PILOT_HEAD)
        self.assertEqual(observed["job"]["candidate"], CANDIDATE)
        self.assertEqual(observed["job"]["rounds"], 3)
        self.assertEqual(observed["provenance"]["kind"], "merge_from_base")
        self.assertEqual(observed["provenance"]["source"], "github_commit")
        self.assertEqual(observed["provenance"]["parents"], [CANDIDATE, BASE])
        self.assertEqual(observed["ci"]["checks"]["check:11"]["phase"], "in_progress")
        self.assertEqual([entry["sha"] for entry in self.events() if entry.get("kind") == "ci_started"], [PILOT_HEAD])
        self.assertEqual(json.loads(self.db.execute("SELECT data FROM jobs WHERE number=10").fetchone()[0])["rounds"], 3)

    def test_failure_details_retry_and_filter_and_other_conclusions_are_distinct(self):
        conclusions = ["failure", "timed_out", "cancelled", "action_required", "skipped", "neutral"]
        self.github.checks[PILOT_HEAD] = [
            check(11 + i, "completed", conclusion, name=f"check-{i}")
            for i, conclusion in enumerate(conclusions)
        ]
        self.github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 1, "completed", "failure")]
        self.github.jobs[80, 1] = [job(51, 11, PILOT_HEAD, 1, "completed", "failure")]
        log_endpoint = f"{REPO}/actions/jobs/51/logs"
        self.github.failures[log_endpoint, 1] = ProcessError(
            "forbidden", "HTTP/2 403 Forbidden\n\n{}", "HTTP 403",
        )
        self.assertEqual(self.poll(), 1)
        failed = self.status()
        self.assertEqual(failed["errors"]["log:check:11"]["status"], 403)
        self.assertNotIn("log_excerpt", failed["ci"]["checks"]["check:11"]["diagnostic"])
        del self.github.failures[log_endpoint, 1]
        self.assertEqual(self.poll(), 0)
        observed = self.status()
        evidence = observed["ci"]["checks"]["check:11"]
        self.assertEqual(evidence["attempt"], 1)
        self.assertEqual(evidence["run_id"], 80)
        self.assertEqual(evidence["workflow_id"], 3)
        self.assertEqual(evidence["job_id"], 51)
        self.assertEqual(evidence["diagnostic"]["log_url"], f"https://api.github.com/{log_endpoint}")
        self.assertIn("ordinary context", evidence["diagnostic"]["log_excerpt"])
        self.assertIn("failure api_key=[redacted]", evidence["diagnostic"]["annotations"][0]["message"])
        for secret in ("a private value", "fixtureToken", "sk-fixture", "privatefixture", "opaque-fixture"):
            self.assertNotIn(secret, json.dumps(self.events()))
        log = self.events(PILOT_HEAD)
        self.assertEqual({entry["detail"]["conclusion"] for entry in log if entry.get("kind") == "ci_error"}, set(conclusions[:4]))
        self.assertEqual(sum(entry.get("kind") == "intervention_requested" for entry in log), 1)
        self.assertEqual({entry["conclusion"] for entry in observed["ci"]["checks"].values()}, set(conclusions))
        self.assertEqual(sum(entry.get("kind") == "ci_error" and entry["detail"]["key"] == "check:11" for entry in log), 1)

    def test_new_run_rerun_new_head_and_restart_keep_distinct_evidence(self):
        self.insert_job()
        self.github.checks[PILOT_HEAD] = [check(11, "completed", "failure")]
        first = run(80, PILOT_HEAD, 1, "completed", "failure")
        self.github.runs[PILOT_HEAD] = [first]
        self.github.jobs[80, 1] = [job(51, 11, PILOT_HEAD, 1, "completed", "failure")]
        self.assertEqual(self.poll(), 0)
        initial_events = self.events(PILOT_HEAD)
        self.db.close()
        self.db = queue.connect(self.root)
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.events(PILOT_HEAD), initial_events)

        self.github.runs[PILOT_HEAD] = [
            run(81, PILOT_HEAD, 1, "queued"),
            run(80, PILOT_HEAD, 2, "in_progress"),
        ]
        self.github.attempts[80, 1] = first
        self.github.jobs[80, 2] = [job(52, 12, PILOT_HEAD, 2, "in_progress")]
        self.github.checks[PILOT_HEAD] += [check(12, "in_progress")]
        self.assertEqual(self.poll(), 0)
        ci = self.status()["ci"]
        self.assertEqual(set(ci["runs"]), {"80:1", "80:2", "81:1"})
        self.assertFalse(ci["checks"]["check:11"]["current"])
        self.assertEqual(ci["checks"]["check:12"]["attempt"], 2)
        self.assertTrue(ci["runs"]["80:1"]["historical"])
        last_events = self.events(PILOT_HEAD)
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.events(PILOT_HEAD), last_events)

        self.github.pulls = [[pull(sha=NEW_HEAD)]]
        self.github.checks[NEW_HEAD] = [check(14, "in_progress")]
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.status()["head"]["sha"], NEW_HEAD)
        self.assertNotIn("check:11", self.status()["ci"]["checks"])
        self.assertEqual(self.events(PILOT_HEAD), last_events)
        self.assertIn("pr_head", [entry.get("kind") for entry in self.events(NEW_HEAD)])

    def test_late_action_metadata_does_not_duplicate_failure(self):
        failing = check(11, "completed", "failure")
        failing["details_url"] = f"https://github.com/{SLUG}/actions/runs/80/job/51"
        self.github.checks[PILOT_HEAD] = [failing]
        endpoint = f"{REPO}/actions/runs"
        self.github.failures[endpoint, 1] = RuntimeError("temporarily unavailable")
        self.assertEqual(self.poll(), 1)
        evidence = self.status()["ci"]["checks"]["check:11"]
        self.assertIsNone(evidence["attempt"])
        self.assertEqual(evidence["job_id"], 51)
        del self.github.failures[endpoint, 1]
        self.github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 1, "completed", "failure")]
        self.github.jobs[80, 1] = [job(51, 11, PILOT_HEAD, 1, "completed", "failure")]
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.status()["ci"]["checks"]["check:11"]["attempt"], 1)
        events = [entry["kind"] for entry in self.events()
                  if entry.get("detail", {}).get("key") == "check:11"]
        self.assertEqual(events.count("ci_error"), 1)
        self.assertIn("ci_correlated", events)

    def test_late_check_metadata_reuses_job_failure_and_diagnostic(self):
        self.github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 1, "completed", "failure")]
        self.github.jobs[80, 1] = [job(51, 11, PILOT_HEAD, 1, "completed", "failure")]
        self.assertEqual(self.poll(), 0)
        original = self.status()["ci"]["checks"]["job:51"]

        self.github.checks[PILOT_HEAD] = [check(11, "completed", "failure")]
        self.github.failures[f"{REPO}/actions/jobs/51/logs", 1] = AssertionError("already downloaded")
        self.assertEqual(self.poll(), 0)
        observed = self.status()["ci"]["checks"]
        self.assertFalse(observed["job:51"]["current"])
        self.assertEqual(observed["check:11"]["diagnostic"]["log_excerpt"], original["diagnostic"]["log_excerpt"])
        self.assertTrue(observed["check:11"]["diagnostic"]["annotations"])
        failures = [entry for entry in self.events() if entry.get("kind") == "ci_error"]
        self.assertEqual([entry["detail"]["key"] for entry in failures], ["job:51", "80:1"])
        self.assertIn("job:51", [entry["detail"].get("previous_key") for entry in self.events()
                                 if entry.get("kind") == "ci_correlated"])

    def test_reused_check_id_keeps_evidence_for_both_attempts(self):
        self.github.checks[PILOT_HEAD] = [check(11, "completed", "failure")]
        self.github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 1, "completed", "failure")]
        self.github.jobs[80, 1] = [job(51, 11, PILOT_HEAD, 1, "completed", "failure")]
        self.assertEqual(self.poll(), 0)
        first_diagnostic = self.status()["ci"]["checks"]["check:11"]["diagnostic"]

        self.github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 2, "completed", "failure")]
        self.github.jobs[80, 2] = [job(52, 11, PILOT_HEAD, 2, "completed", "failure")]
        self.assertEqual(self.poll(), 0)
        checks = self.status()["ci"]["checks"]
        self.assertFalse(checks["check:11:51:1"]["current"])
        self.assertEqual(checks["check:11:51:1"]["diagnostic"], first_diagnostic)
        self.assertEqual(checks["check:11"]["attempt"], 2)
        self.assertTrue(checks["check:11"]["diagnostic"]["log_url"].endswith("/jobs/52/logs"))
        events = self.events()
        self.assertEqual([entry["detail"]["attempt"] for entry in events
                          if entry.get("kind") == "ci_error" and entry["detail"]["key"] == "check:11"], [1, 2])
        self.db.close()
        self.db = queue.connect(self.root)
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.events(), events)

    def test_failed_historical_attempt_lookup_keeps_state_and_retries(self):
        self.github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 1, "in_progress")]
        self.assertEqual(self.poll(), 0)
        self.github.runs[PILOT_HEAD] = [run(80, PILOT_HEAD, 2, "queued")]
        endpoint = f"{REPO}/actions/runs/80/attempts/1"
        self.github.failures[endpoint, 1] = RuntimeError("attempt unavailable")
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.status()["ci"]["runs"]["80:1"]["phase"], "in_progress")
        self.assertIn("run:80:1", self.status()["errors"])

        del self.github.failures[endpoint, 1]
        self.github.attempts[80, 1] = run(80, PILOT_HEAD, 1, "completed", "failure")
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.status()["ci"]["runs"]["80:1"]["phase"], "completed")
        self.assertFalse(self.status()["errors"])
        evidence = self.events()
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.events(), evidence)

    def test_registered_url_alone_can_find_pr_already_closed_at_first_poll(self):
        self.insert_job(pr_number=None, pr=f"https://github.com/{SLUG}/pull/23")
        self.github.pulls = [[]]
        self.github.details[23] = pull(state="closed", merged=True)
        del self.github.details[23]["merged_at"]
        self.github.details[23]["merged"] = True
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.status()["job"]["method"], "registered")
        self.assertEqual(self.status()["state"], "merged")
        self.assertEqual(self.db.execute("SELECT state FROM jobs").fetchone()[0], "merged")

    def test_disappearance_needs_complete_list_and_confirmed_detail_for_close_and_merge(self):
        self.insert_job()
        self.insert_job(number=20, pr_number=24)
        self.github.pulls = [[pull(), pull(24, CANDIDATE)]]
        self.assertEqual(self.poll(), 0)
        self.github.pulls = [[], []]
        self.github.failures[f"{REPO}/pulls", 2] = RuntimeError("page unavailable")
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.status()["state"], "open")
        self.assertFalse(any(path.startswith(f"{REPO}/pulls/") for path, _ in self.github.requests))
        del self.github.failures[f"{REPO}/pulls", 2]
        self.github.failures[f"{REPO}/pulls/23", 1] = RuntimeError("detail unavailable")
        self.github.details[24] = pull(24, CANDIDATE, state="closed", merged=True)
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.status()["errors"]["pr_detail"]["message"], "detail unavailable")
        self.assertEqual(self.status(24)["state"], "merged")
        del self.github.failures[f"{REPO}/pulls/23", 1]
        self.github.details[23] = pull(state="closed")
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.status()["state"], "closed")
        self.assertEqual(
            [row["state"] for row in self.db.execute("SELECT state FROM jobs ORDER BY number")],
            ["closed", "merged"],
        )
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    def test_rate_limit_and_errors_retain_valid_observation_and_rules(self):
        self.github.checks[PILOT_HEAD] = [check(11, "in_progress")]
        self.assertEqual(self.poll(), 0)
        original = copy.deepcopy(self.status()["ci"])
        self.github.failures[f"{REPO}/commits/{PILOT_HEAD}/check-runs", 1] = ProcessError(
            "rate limit", "HTTP/2 429 Too Many Requests\nRetry-After: 60\n\n{}", "HTTP 429",
        )
        self.assertGreater(self.poll(), 0)
        unchanged = self.status()
        self.assertEqual(unchanged["ci"]["checks"], original["checks"])
        self.assertFalse(unchanged["ci"]["required"]["known"])
        self.assertEqual(unchanged["ci"]["required"]["checks"], original["required"]["checks"])
        self.assertEqual(unchanged["errors"]["check_runs"]["status"], 429)
        self.assertEqual(self.github.requests[-1][0], f"{REPO}/commits/{PILOT_HEAD}/check-runs")
        self.assertGreater(queue.retry_delay(), 0)
        del self.github.failures[f"{REPO}/commits/{PILOT_HEAD}/check-runs", 1]
        queue._retry_at = 0
        self.assertEqual(self.poll(), 0)
        self.assertFalse(self.status()["errors"])
        self.assertTrue(any(entry.get("kind") == "observation_recovered" for entry in self.events()))
