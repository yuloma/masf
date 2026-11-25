"""Persist the controller's pull request and CI observations in SQLite."""

import json
import re
from datetime import datetime, timezone

from . import github_ci as gh
from .evidence import timestamp
from .queue import decision_details, pages, retry_delay, write_event
from .system import filtered


def load_row(row):
    return json.loads(row["data"]) if row else None


def pr_ref(ref):
    repo = ref.get("repo") or {}
    return {
        "repo_id": repo.get("id"),
        "repo": repo.get("full_name"),
        "ref": ref["ref"],
        "sha": ref["sha"],
    }


def pull_identity(slug, item):
    if item["state"] not in {"open", "closed"}:
        raise ValueError("Estado de PR desconocido")
    base, head = pr_ref(item["base"]), pr_ref(item["head"])
    if not base["repo_id"] or not head["sha"]:
        raise ValueError("El PR carece de repositorio base o SHA")
    return {
        "version": 1,
        "repo_id": base["repo_id"],
        "slug": slug,
        "number": item["number"],
        "id": item["id"],
        "url": item["html_url"],
        "state": "merged" if item.get("merged") or item.get("merged_at") else item["state"],
        "draft": item.get("draft"),
        "base": base,
        "head": head,
    }


def registered_pull_number(job, slug):
    if job.get("pr_number"):
        return job["pr_number"]
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(slug)}/pull/([1-9][0-9]*)/?",
        job.get("pr") or "", re.IGNORECASE,
    )
    return int(match[1]) if match else None


def link_job(db, pr, body):
    rows = db.execute("SELECT data FROM jobs WHERE repo_id=?", (pr["repo_id"],))
    jobs = [load_row(row) for row in rows]
    for job in jobs:
        if registered_pull_number(job, pr["slug"]) == pr["number"]:
            return job, "registered"
    link = db.execute(
        "SELECT job_number FROM pull_links WHERE repo_id=? AND number=?",
        (pr["repo_id"], pr["number"]),
    ).fetchone()
    if link:
        for job in jobs:
            if job["number"] == link["job_number"] and not (job.get("pr_number") or job.get("pr")):
                return job, "operator"
    head, base = pr["head"], pr["base"]
    if head["repo_id"] != base["repo_id"]:
        return None, None
    for job in jobs:
        owner = job.get("owner")
        if (
            owner and f"<!-- masf:{owner} -->" in (body or "")
            and head["ref"] == f"masf/issue-{job['number']}"
            and head["sha"] in {job.get("candidate"), job.get("intent")}
            and base["ref"] == job.get("branch")
        ):
            return job, "publication"
    return None, None


def job_ref(job, method):
    return gh.fields(job, "number", "candidate", "rounds") | {"method": method}


def link_pull(db, target, issue, *, operator=None):
    slug, number = target
    if slug.lower() != issue[0].lower():
        raise ValueError("El PR y la issue deben pertenecer al mismo repositorio")
    with db:
        db.execute("BEGIN IMMEDIATE")
        pr = load_row(db.execute(
            "SELECT data FROM pull_requests WHERE slug=? COLLATE NOCASE AND number=?", target,
        ).fetchone())
        if pr is None:
            raise ValueError("PR aún sin observación")
        job = load_row(db.execute(
            "SELECT data FROM jobs WHERE repo_id=? AND number=?", (pr["repo_id"], issue[1]),
        ).fetchone())
        if job is None:
            raise ValueError("Trabajo sin registrar")
        current = pr["job"]
        if current and current["number"] == job["number"]:
            return
        if current or job.get("pr_number") or job.get("pr"):
            raise ValueError("El PR o el trabajo ya tiene un vínculo")
        existing = db.execute(
            """SELECT 1 FROM pull_requests WHERE repo_id=? AND json_extract(data,'$.job.number')=?
            UNION ALL SELECT 1 FROM pull_links WHERE repo_id=? AND job_number=?""",
            (pr["repo_id"], issue[1], pr["repo_id"], issue[1]),
        ).fetchone()
        if existing:
            raise ValueError("El trabajo ya tiene un vínculo")
        db.execute("INSERT INTO pull_links VALUES(?,?,?)", (pr["repo_id"], number, issue[1]))
        pr["job"] = job_ref(job, "operator")
        entry = {"kind": "link_observation", "operator": operator, "at": timestamp(),
                 "head": pr["head"]["sha"], "pr": number,
                 "evidence": "Asociación solicitada por el operador"}
        operations = decision_details([entry], job)
        append_event(db, pr, "pr_job", {"before": None, "after": issue[1], "method": "operator",
                                        "operations": operations})
        write_event(db, job, job["state"], {"operations": operations})
        db.execute("UPDATE pull_requests SET data=? WHERE repo_id=? AND number=?", (
            json.dumps(filtered(pr)), pr["repo_id"], number,
        ))


def provenance(request, repo, pr, previous, errors):
    job = pr["job"]
    candidate = job["candidate"] if job else None
    sha, base = pr["head"]["sha"], pr["base"]
    if not candidate or candidate == sha:
        return {"kind": "candidate" if candidate else "unknown"}
    dependencies = {
        "candidate": candidate, "base_sha": base["sha"],
        "base_ref": base["ref"], "head_sha": sha,
    }
    prior = (previous or {}).get("provenance", {})
    if all(prior.get(key) == value for key, value in dependencies.items()) and not (
        (previous or {}).get("errors", {}).get("provenance")
    ):
        return prior

    def inspect():
        commit = request(f"{repo}/commits/{sha}")
        if commit["sha"] != sha:
            raise ValueError("GitHub devolvió un commit distinto del head")
        parents = [parent["sha"] for parent in commit["parents"]]
        result = dependencies | {"kind": "different_head", "parents": parents, "source": "github_commit"}
        if len(parents) != 2:
            return result

        def ancestor(older, newer):
            if older == newer:
                return True
            comparison = request(f"{repo}/compare/{older}...{newer}")
            return comparison["status"] in {"ahead", "identical"} and (
                comparison["merge_base_commit"]["sha"] == older
            )

        if ancestor(candidate, parents[0]) and ancestor(parents[1], base["sha"]):
            result.update(kind="merge_from_base", merged_sha=parents[1])
        return result

    fallback = dependencies | {"kind": "different_head"}
    return gh.query(errors, "provenance", inspect, fallback)


def phase(status):
    return {
        "pending": "pending", "waiting": "pending", "requested": "pending",
        "queued": "queued", "in_progress": "in_progress", "completed": "completed",
    }.get(status, "unknown")


def run_records(request, repo, sha, runs, prior, errors):
    records = {key: value | {"current": False} for key, value in prior.items()}
    jobs_by_check = {}
    for run_id, latest in runs.items():
        latest_attempt = latest["run_attempt"]
        for attempt in range(1, (latest_attempt or 1) + 1):
            key = f"{run_id}:{attempt if latest_attempt is not None else 'unknown'}"
            old = prior.get(key, {})
            historical = latest_attempt is not None and attempt < latest_attempt
            endpoint = f"{repo}/actions/runs/{run_id}/attempts/{attempt}"
            if historical:
                run = old.get("run")
                if (run or {}).get("status") != "completed":
                    run = gh.query(errors, f"run:{key}", lambda: gh.attempt(
                        request, endpoint, sha, latest, attempt,
                    ), run)
            else:
                run = latest
            old_jobs = old.get("jobs")
            # A complete job listing collected after a run finished is immutable.
            if old.get("jobs_complete") and (historical or (old.get("run") or {}).get("status") == "completed"):
                found = old_jobs
            else:
                found = gh.query(errors, f"jobs:{key}", lambda: gh.jobs(
                    request, repo, sha, run_id, attempt if latest_attempt is not None else None,
                ))
            jobs = found if found is not None else (old_jobs or {})
            run_link = (run or latest).get("html_url")
            workflow = latest["workflow_id"]
            records[key] = {
                "run": run,
                "run_id": latest["id"],
                "workflow_id": workflow,
                "workflow_url": f"https://api.github.com/{repo}/actions/workflows/{workflow}" if workflow else None,
                "url": run_link,
                "attempt_url": f"https://api.github.com/{endpoint}" if latest_attempt else None,
                "log_url": (
                    f"https://api.github.com/{endpoint}/logs" if latest_attempt
                    else f"https://api.github.com/{repo}/actions/runs/{run_id}/logs"
                ),
                "attempt": attempt if latest_attempt else None,
                "historical": historical,
                "current": not historical,
                "phase": phase((run or {}).get("status")),
                "jobs": jobs,
                "jobs_complete": found is not None and (
                    (historical or (run or {}).get("status") == "completed")
                    and all(job["status"] == "completed" for job in jobs.values())
                ),
            }
            for job in jobs.values():
                check = gh.check_id(job)
                if check is not None:
                    jobs_by_check[check] = (key, job)
    return records, jobs_by_check


def diagnose(request, repo, check, errors):
    key = check["key"]
    result = dict(check.get("diagnostic", {}))
    if check["source"] == "check" and key.count(":") == 1:
        endpoint = f"{repo}/check-runs/{check['id']}"
        result.setdefault("annotations_url", f"https://api.github.com/{endpoint}/annotations")
        if "annotations" not in result:
            value = gh.query(errors, f"annotations:{key}", lambda: gh.annotation_excerpt(
                request, f"{endpoint}/annotations",
            ))
            if value is not None:
                result["annotations"] = value
        if "output" not in result:
            output = gh.query(errors, f"output:{key}", lambda: request(endpoint).get("output") or {})
            if output is not None:
                result["output"] = {
                    name: gh.excerpt(output.get(name), 4000)
                    for name in ("title", "summary", "text")
                }
    if check.get("job_id") is not None:
        endpoint = f"{repo}/actions/jobs/{check['job_id']}/logs"
        result.setdefault("log_url", f"https://api.github.com/{endpoint}")
        if "log_excerpt" not in result:
            value = gh.query(errors, f"log:{key}", lambda: gh.log_excerpt(request(endpoint, raw=True)))
            if value is not None:
                result["log_excerpt"] = value
    if check.get("details_url") or check.get("target_url"):
        result["details_url"] = check.get("details_url") or check["target_url"]
    return result


def previous_check(prior, key, check):
    if key in prior:
        return prior[key]
    if check["source"] == "check" and key.count(":") > 1:
        return prior.get(key.rsplit(":", 2)[0], {})
    if check["source"] == "check" and check.get("job_id") is not None:
        old = prior.get(f"job:{check['job_id']}", {})
        if old.get("attempt") in (None, check.get("attempt")):
            return old
    return {}


def observe_ci(request, repo, pr, old, previous_rules, rules_cache, errors):
    sha = pr["head"]["sha"]
    checks = gh.query(errors, "check_runs", lambda: gh.checks(request, repo, sha))
    statuses = gh.query(errors, "commit_statuses", lambda: gh.statuses(request, repo, sha))
    runs = gh.query(errors, "workflow_runs", lambda: gh.actions(request, repo, sha))
    run_history, job_matches = (
        run_records(request, repo, sha, runs, old.get("runs", {}), errors)
        if runs is not None else (old.get("runs", {}), {})
    )

    prior = old.get("checks", {})
    observed = {}
    for key, check in prior.items():
        source = {"status": statuses, "check": checks, "job": runs}[check["source"]]
        observed[key] = check if source is None else check | {"current": False}

    for key, check in (checks or {}).items():
        match = job_matches.get(check["id"])
        metadata = {}
        if match:
            run_key, job = match
            run = run_history[run_key]
            metadata = {
                "run_id": run["run_id"], "workflow_id": run["workflow_id"],
                "job_id": job["id"], "job_url": job["html_url"],
                "run_url": run["url"], "attempt": run["attempt"] or job.get("attempt"),
            }
            check["current"] = run["current"]
        else:
            metadata = correlate_from_check(repo, check, prior.get(key, {}), runs or {})
            check["current"] = prior.get(key, {}).get("current", True) if metadata["job_id"] else True
        old_check = prior.get(key, {})
        if any(
            old_check.get(field) is not None and metadata[field] is not None
            and old_check[field] != metadata[field]
            for field in ("attempt", "job_id")
        ):
            archive = f"{key}:{old_check.get('job_id')}:{old_check.get('attempt')}"
            observed[archive] = old_check | {"key": archive, "current": False}
            if not match:
                check["current"] = True
        observed[key] = check | metadata

    if runs is not None:
        observed_check_ids = {
            item["id"] for item in observed.values() if item["source"] == "check"
        }
        for run in run_history.values():
            for job in run["jobs"].values():
                if gh.check_id(job) in observed_check_ids:
                    continue
                observed[f"job:{job['id']}"] = job | {
                    "source": "job", "job_id": job["id"], "job_url": job["html_url"],
                    "run_id": run["run_id"], "workflow_id": run["workflow_id"],
                    "run_url": run["url"], "current": run["current"],
                }

    observed.update(statuses or {})
    for key, check in observed.items():
        check = observed[key] = check | {"key": key, "phase": phase(check["status"])}
        if check["conclusion"] in gh.ERROR_CONCLUSIONS:
            previous = previous_check(prior, key, check)
            if (
                previous.get("conclusion") == check["conclusion"]
                and previous.get("attempt") in (None, check.get("attempt"))
                and previous.get("job_id") in (None, check.get("job_id"))
            ):
                check["diagnostic"] = previous.get("diagnostic", {})
            check["diagnostic"] = diagnose(request, repo, check, errors)

    branch = pr["base"]["ref"]
    if branch not in rules_cache:
        failures = {}
        fetched = gh.branch_requirements(request, repo, branch, failures)
        rules_cache[branch] = fetched, failures
    fetched, failures = rules_cache[branch]
    errors.update(failures)
    old_rules = old.get("required", {}) if old.get("base_ref") == branch else {}
    required = gh.required_checks(fetched, old_rules or previous_rules)
    if checks is None or statuses is None:
        missing = old_rules.get("missing")
        unverified = old_rules.get("unverified")
    else:
        missing, unverified = [], []
        for rule in required["checks"]:
            matches = [check for check in observed.values() if check["current"] and check["name"] == rule["name"]]
            app = rule["app_id"]
            if not matches:
                missing.append(rule)
            elif app and not any(check.get("app_id") == app for check in matches):
                target = unverified if any(check.get("app_id") is None for check in matches) else missing
                target.append(rule)
    required.update(missing=missing, unverified=unverified)
    return {"base_ref": branch, "checks": observed, "runs": run_history, "required": required}


def correlate_from_check(repo, check, old, runs):
    metadata = gh.fields(old, "run_id", "workflow_id", "job_id", "job_url", "run_url", "attempt")
    direct = gh.action_job(check["details_url"], repo)
    if direct:
        run_id, job_id = direct
        if (run_id, job_id) != (metadata["run_id"], metadata["job_id"]):
            if run_id != metadata["run_id"]:
                metadata.update(workflow_id=None, run_url=None)
            metadata.update(run_id=run_id, job_id=job_id, job_url=check["details_url"], attempt=None)
    suite = check["suite_id"]
    candidates = [
        run for run in runs.values()
        if run["id"] == metadata["run_id"] or (suite and run["check_suite_id"] == suite)
    ]
    if len(candidates) == 1:
        run = candidates[0]
        metadata.update(run_id=run["id"], workflow_id=run["workflow_id"], run_url=run["html_url"])
        if run["run_attempt"] == 1:
            metadata["attempt"] = 1
    return metadata


def append_event(db, pr, kind, detail):
    db.execute(
        "INSERT INTO pull_events(repo_id,number,sha,kind,detail) VALUES(?,?,?,?,?)",
        (pr["repo_id"], pr["number"], pr["head"]["sha"], kind, json.dumps(filtered(detail))),
    )


def result_events(db, pr, old, new, key, *, source):
    state = (new["phase"], new["conclusion"], new.get("attempt"), new.get("job_id"))
    previous = (old.get("phase"), old.get("conclusion"), old.get("attempt"), old.get("job_id"))
    detail = gh.fields(
        new, "id", "name", "status", "phase", "conclusion", "attempt",
        "run_id", "workflow_id", "job_id", "completed_at",
    )
    detail["started_at"] = new.get("started_at") or new.get("run_started_at")
    detail.update(source=source, key=key)
    correlated_job = source == "check" and old.get("source") == "job"
    if correlated_job:
        detail["previous_key"] = old["key"]
    if state != previous or correlated_job:
        different_attempt = any(
            before is not None and after is not None and before != after
            for before, after in zip(previous[2:], state[2:])
        )
        if state[:2] != previous[:2] or different_attempt:
            kind = {"in_progress": "ci_started", "completed": "ci_finished"}.get(state[0], "ci_state")
        else:
            kind = "ci_correlated"
        if kind == "ci_finished" and detail["started_at"] and (
            not old or different_attempt or previous[0] in {"pending", "queued", "unknown"}
        ):
            append_event(db, pr, "ci_started", detail | {
                "status": "in_progress", "phase": "in_progress", "conclusion": None,
                "completed_at": None, "inferred": True,
            })
        append_event(db, pr, kind, detail)
        if kind == "ci_finished" and new["conclusion"] in gh.ERROR_CONCLUSIONS:
            append_event(db, pr, "ci_error", detail)
        if kind == "ci_finished" and new["conclusion"] == "action_required":
            append_event(db, pr, "intervention_requested", detail)


def error_events(db, pr, old, new):
    for source in sorted(old.keys() | new.keys()):
        if old.get(source) != new.get(source):
            append_event(
                db, pr, "observation_error" if source in new else "observation_recovered",
                {"source": source, "error": new.get(source)},
            )


def close_job(db, pr):
    if not pr["job"] or pr["state"] not in {"closed", "merged"}:
        return
    ident = pr["repo_id"], pr["job"]["number"]
    row = db.execute(
        """SELECT data FROM jobs WHERE repo_id=? AND number=?
        AND (state='pr_open' OR (state='closed' AND ?='merged'))
        AND NOT EXISTS (SELECT 1 FROM leases WHERE leases.repo_id=jobs.repo_id
                        AND leases.number=jobs.number AND expires>unixepoch())""",
        (*ident, pr["state"]),
    ).fetchone()
    job = load_row(row)
    if job is None or registered_pull_number(job, pr["slug"]) != pr["number"]:
        return
    job.pop("attempt", None)
    job["state"] = pr["state"]
    job["integration_approval"] = None
    db.execute("UPDATE jobs SET state=?,data=? WHERE repo_id=? AND number=?", (
        pr["state"], json.dumps(filtered(job)), *ident,
    ))
    db.execute("INSERT INTO events(repo_id,number,state,detail) VALUES(?,?,?,?)", (
        *ident, pr["state"], json.dumps({"pr_number": pr["number"], "sha": pr["head"]["sha"]}),
    ))


def persist(db, pr, ci=None):
    pr = filtered(pr)
    ci = filtered(ci)
    ident = pr["repo_id"], pr["number"]
    sha = pr["head"]["sha"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = load_row(db.execute(
            "SELECT data FROM pull_requests WHERE repo_id=? AND number=?", ident,
        ).fetchone()) or {}
        previous = load_row(db.execute(
            "SELECT data FROM pull_ci WHERE repo_id=? AND number=? AND sha=?", (*ident, sha),
        ).fetchone()) or {}
        if not old:
            append_event(db, pr, "pr_discovered", pr)
        else:
            for field, event in (
                ("head", "pr_head"), ("state", "pr_state"), ("draft", "pr_draft"),
                ("base", "pr_base"), ("job", "pr_job"), ("provenance", "pr_provenance"),
            ):
                if field == "job":
                    # A change in local rounds is visible without changing the association.
                    old_value, new_value = (
                        (item.get(field) or {}).get("number") for item in (old, pr)
                    )
                else:
                    old_value, new_value = old.get(field), pr.get(field)
                if old_value != new_value:
                    append_event(db, pr, event, {"before": old_value, "after": new_value})
        error_events(
            db, pr,
            {key: value for key, value in old.get("errors", {}).items() if key == "pr_detail"},
            {key: value for key, value in pr["errors"].items() if key == "pr_detail"},
        )
        if ci is not None:
            observed_at = datetime.now(timezone.utc).isoformat()
            ci["observed_at"] = observed_at
            ci["last_success_at"] = (previous.get("last_success_at") or {}) | {
                source: observed_at for source in (
                    "check_runs", "commit_statuses", "workflow_runs", "branch_protection", "branch_rules",
                ) if source not in pr["errors"]
            }
            pr["observed_at"] = observed_at
            ci["errors"] = pr["errors"]
            error_events(db, pr, previous.get("errors", {}), ci["errors"])
            for key, result in ci["checks"].items():
                old_result = previous_check(previous.get("checks", {}), key, result)
                result_events(db, pr, old_result, result, key, source=result["source"])
                if result.get("diagnostic") != old_result.get("diagnostic") and result.get("diagnostic"):
                    append_event(db, pr, "ci_diagnostic", gh.fields(
                        result, "key", "id", "source", "run_id", "job_id", "attempt", "diagnostic",
                    ))
            for key, record in ci["runs"].items():
                if record["run"] is not None:
                    old_run = (previous.get("runs", {}).get(key, {}).get("run") or {})
                    result = record["run"] | {
                        "attempt": record["attempt"], "run_id": record["run_id"],
                        "phase": record["phase"],
                    }
                    if old_run:
                        old_run = old_run | {
                            "attempt": previous["runs"][key]["attempt"],
                            "phase": previous["runs"][key]["phase"],
                        }
                    result_events(db, pr, old_run, result, key, source="workflow")
            if ci["required"] != previous.get("required"):
                append_event(db, pr, "required_checks", ci["required"])
            db.execute("""INSERT INTO pull_ci VALUES(?,?,?,?)
                ON CONFLICT(repo_id,number,sha) DO UPDATE SET data=excluded.data""",
                (*ident, sha, json.dumps(ci)),
            )
        db.execute("""INSERT INTO pull_requests VALUES(?,?,?,?,?,?)
            ON CONFLICT(repo_id,number) DO UPDATE SET slug=excluded.slug,state=excluded.state,
            sha=excluded.sha,data=excluded.data""",
            (*ident, pr["slug"], pr["state"], sha, json.dumps(filtered(pr))),
        )
        if ci is not None:
            close_job(db, pr)


def observe_pull(db, request, slug, item, expected_id, rules_cache, expected_number=None):
    pr = pull_identity(slug, item)
    if pr["repo_id"] != expected_id or (
        expected_number is not None and pr["number"] != expected_number
    ) or (expected_number is None and pr["state"] != "open"):
        raise ValueError("GitHub devolvió un PR distinto del solicitado")
    repo = f"repos/{slug}"
    key = pr["repo_id"], pr["number"]
    job, method = link_job(db, pr, item.get("body"))
    pr["job"] = job_ref(job, method) if job else None
    previous = load_row(db.execute(
        "SELECT data FROM pull_requests WHERE repo_id=? AND number=?", key,
    ).fetchone())
    old_ci = load_row(db.execute(
        "SELECT data FROM pull_ci WHERE repo_id=? AND number=? AND sha=?",
        (*key, pr["head"]["sha"]),
    ).fetchone()) or {}
    previous_rules = {}
    if not old_ci and previous and previous["base"]["ref"] == pr["base"]["ref"]:
        previous_ci = load_row(db.execute(
            "SELECT data FROM pull_ci WHERE repo_id=? AND number=? AND sha=?",
            (*key, previous["head"]["sha"]),
        ).fetchone())
        previous_rules = (previous_ci or {}).get("required", {})
    errors = pr["errors"] = {}
    pr["provenance"] = provenance(request, repo, pr, previous, errors)
    ci = observe_ci(request, repo, pr, old_ci, previous_rules, rules_cache, errors)
    persist(db, pr, ci)
    return len(errors)


def observe_repository(db, request, slug, meta=None):
    errors = {}
    repo = f"repos/{slug}"
    if meta is None:
        meta = gh.query(errors, "repository", lambda: request(repo)["id"])
    pulls = None
    if meta is not None:
        pulls = gh.query(errors, "list", lambda: {
            item["number"]: item for item in pages(request, f"{repo}/pulls?state=open")
        })
    pull_errors = 0
    if pulls is not None:
        rules_cache = {}
        tracked = {
            row["number"] for row in db.execute(
                "SELECT number FROM pull_requests WHERE repo_id=? AND state='open'", (meta,),
            )
        }
        tracked.update(
            number
            for job in map(load_row, db.execute(
                "SELECT data FROM jobs WHERE repo_id=? AND state='pr_open'", (meta,),
            ))
            if (number := registered_pull_number(job, slug)) is not None
        )
        for number, item in pulls.items():
            pull_errors += gh.query(errors, f"pull:{number}", lambda: observe_pull(
                db, request, slug, item, meta, rules_cache,
            ), 0)
        for number in sorted(tracked - pulls.keys()):
            source = f"pull:{number}"
            item = gh.query(errors, source, lambda: request(f"{repo}/pulls/{number}"))
            if item is not None:
                pull_errors += gh.query(errors, source, lambda: observe_pull(
                    db, request, slug, item, meta, rules_cache, number,
                ), 0)
            if source in errors:
                prior = load_row(db.execute(
                    "SELECT data FROM pull_requests WHERE repo_id=? AND number=?", (meta, number),
                ).fetchone())
                if prior:
                    prior["errors"]["pr_detail"] = errors[source]
                    persist(db, prior)
    with db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT error FROM pull_sources WHERE slug=?", (slug,)).fetchone()
        old_errors = json.loads(previous["error"]) if previous else {}
        if pulls is None:
            errors.update({key: value for key, value in old_errors.items() if key.startswith("pull:")})
        if meta is None and "list" in old_errors:
            errors["list"] = old_errors["list"]
        if old_errors != errors:
            db.execute("""INSERT INTO pull_sources(slug,error) VALUES(?,?)
                ON CONFLICT(slug) DO UPDATE SET error=excluded.error,changed_at=CURRENT_TIMESTAMP""",
                (slug, json.dumps(filtered(errors))),
            )
            for source in sorted(old_errors.keys() | errors.keys()):
                if old_errors.get(source) != errors.get(source):
                    db.execute("INSERT INTO pull_source_events(slug,source,error) VALUES(?,?,?)", (
                        slug, source, json.dumps(filtered(errors[source])) if source in errors else None,
                    ))
    return len(errors) + pull_errors


def observe(db, request, cfg, repository_ids):
    errors = 0
    for repo in cfg["repositories"]:
        if repo["enabled"]:
            slug = repo["slug"]
            errors += observe_repository(db, request, slug, repository_ids.get(slug))
            if retry_delay():
                break
    return errors
