"""Read the GitHub resources that describe CI for one pull request head."""

import re
from urllib.parse import quote, urlencode, urlsplit

from .queue import http_status, pages
from .system import clean


QUERY_ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError)
ERROR_CONCLUSIONS = {
    "failure", "timed_out", "cancelled", "action_required", "startup_failure",
    "stale", "error",
}
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def excerpt(value, limit):
    return clean(ANSI_ESCAPE.sub("", value))[:limit] if isinstance(value, str) else None


def fields(item, *names):
    return {name: item.get(name) for name in names}


def query(errors, source, fetch, fallback=None):
    try:
        return fetch()
    except QUERY_ERRORS as exc:
        # Local backoff errors contain a countdown. Keep their identity stable.
        message = str(exc)
        if message.startswith("GitHub: límite de peticiones"):
            message = "GitHub: espera por límite de peticiones"
        errors[source] = {"status": http_status(exc), "message": clean(message)[:1800]}
        return fallback


def checks(request, repo, sha):
    observed = {}
    endpoint = f"{repo}/commits/{sha}/check-runs?filter=all"
    for item in pages(request, endpoint, "check_runs"):
        if item.get("head_sha", sha) != sha:
            raise ValueError("GitHub devolvió un check de otro SHA")
        output = item.get("output") or {}
        observed[f"check:{item['id']}"] = fields(
            item, "id", "name", "status", "conclusion", "started_at", "completed_at",
            "html_url", "url",
        ) | {
            "source": "check",
            "details_url": excerpt(item.get("details_url"), 2000),
            "suite_id": (item.get("check_suite") or {}).get("id"),
            "app_id": (item.get("app") or {}).get("id"),
            "output": {
                "title": excerpt(output.get("title"), 1000),
                "annotations_count": output.get("annotations_count"),
            },
        }
    return observed


def statuses(request, repo, sha):
    observed = {}
    contexts = set()
    for item in pages(request, f"{repo}/commits/{sha}/statuses"):
        context = item["context"]
        state = item["state"]
        observed[f"status:{item['id']}"] = fields(
            item, "id", "url", "created_at", "updated_at",
        ) | {
            "source": "status",
            "name": context,
            "description": excerpt(item.get("description"), 1000),
            "target_url": excerpt(item.get("target_url"), 2000),
            "creator": fields(item.get("creator") or {}, "id", "login"),
            "github_state": state,
            "status": "pending" if state == "pending" else "completed",
            "conclusion": state if state != "pending" else None,
            "current": context not in contexts,
        }
        contexts.add(context)
    return observed


def actions(request, repo, sha):
    return {
        str(run["id"]): fields(
            run, "id", "workflow_id", "check_suite_id", "name", "path", "run_number",
            "run_attempt", "status", "conclusion", "html_url", "url",
            "run_started_at", "updated_at", "jobs_url",
        )
        for run in pages(
            request, f"{repo}/actions/runs?{urlencode({'head_sha': sha})}", "workflow_runs"
        )
        if run["head_sha"] == sha
    }


def attempt(request, endpoint, sha, latest, number):
    run = request(endpoint)
    if (run["id"], run["head_sha"], run["run_attempt"]) != (latest["id"], sha, number):
        raise ValueError("GitHub devolvió un intento de workflow distinto del solicitado")
    return fields(run, *latest.keys())


def jobs(request, repo, sha, run_id, attempt):
    route = f"{repo}/actions/runs/{run_id}"
    if attempt is not None:
        route += f"/attempts/{attempt}"
    endpoint = f"{route}/jobs" + ("?filter=latest" if attempt is None else "")
    observed = {}
    for job in pages(request, endpoint, "jobs"):
        reported_attempt = job.get("run_attempt")
        if (
            job["head_sha"] != sha
            or str(job.get("run_id", run_id)) != run_id
            or (attempt is not None and reported_attempt not in (None, attempt))
        ):
            raise ValueError("GitHub devolvió un job de otro SHA, ejecución o intento")
        observed[str(job["id"])] = fields(
            job, "id", "name", "status", "conclusion", "html_url", "url", "check_run_url",
            "started_at", "completed_at",
        ) | {"attempt": reported_attempt or attempt}
    return observed


def check_id(job):
    tail = urlsplit(job.get("check_run_url") or "").path.rsplit("/", 1)[-1]
    return int(tail) if tail.isdecimal() else None


def action_job(details_url, repo):
    try:
        url = urlsplit(details_url or "")
    except ValueError:
        return None
    if url.hostname != "github.com":
        return None
    pattern = rf"/{re.escape(repo.removeprefix('repos/'))}/actions/runs/(\d+)/job/(\d+)/?"
    match = re.fullmatch(pattern, url.path, re.IGNORECASE)
    return tuple(map(int, match.groups())) if match else None


def branch_requirements(request, repo, branch, errors):
    endpoint = f"{repo}/branches/{quote(branch, safe='')}"

    def protection():
        try:
            required = request(f"{endpoint}/protection/required_status_checks")
        except QUERY_ERRORS as exc:
            if http_status(exc) != 404:
                raise
            if request(endpoint)["protected"] is False:
                return []
            # The full protection object can confirm a branch has no required checks.
            required = request(f"{endpoint}/protection").get("required_status_checks") or {}
        checks_with_app = required.get("checks", [])
        named_checks = [(item["context"], item.get("app_id")) for item in checks_with_app]
        named = {item["context"] for item in checks_with_app}
        return named_checks + [
            (context, None) for context in required.get("contexts", []) if context not in named
        ]

    def rules():
        rules = pages(request, f"{repo}/rules/branches/{quote(branch, safe='')}")
        return [
            (item["context"], item.get("integration_id"))
            for rule in rules if rule["type"] == "required_status_checks"
            for item in rule["parameters"]["required_status_checks"]
        ]

    return {
        "protection": query(errors, "branch_protection", protection),
        "rules": query(errors, "branch_rules", rules),
    }


def required_checks(fetched, previous):
    sources = previous.get("sources", {}) | {
        source: pairs for source, pairs in fetched.items() if pairs is not None
    }
    expected = {
        (name, app if app and app > 0 else None)
        for requirements in sources.values() for name, app in requirements
    }
    return {
        "known": all(pairs is not None for pairs in fetched.values()),
        "sources": {
            source: sorted([list(pair) for pair in pairs], key=lambda item: (item[0], item[1] or 0))
            for source, pairs in sources.items()
        },
        "checks": [
            {"name": name, "app_id": app}
            for name, app in sorted(expected, key=lambda item: (item[0], item[1] or 0))
        ],
    }


def annotation_excerpt(request, endpoint):
    annotations = []
    for item in pages(request, endpoint):
        entry = fields(item, "path", "start_line", "end_line", "annotation_level", "title", "message", "raw_details")
        annotations.append({
            key: excerpt(value, 1200) if isinstance(value, str) else value
            for key, value in entry.items()
        })
        if len(annotations) == 40:
            break
    return annotations


def log_excerpt(raw):
    raw = ANSI_ESCAPE.sub("", raw)
    masks = set()
    for line in raw.splitlines():
        if "::add-mask::" in line:
            encoded = line.split("::add-mask::", 1)[1]
            decoded = encoded.replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")
            masks.update(value for value in (encoded, decoded, *decoded.splitlines()) if value)
    if masks:
        pattern = "|".join(re.escape(value) for value in sorted(masks, key=lambda v: (-len(v), v)))
        raw = re.sub(pattern, "[redacted]", raw)
    lines = clean(raw).splitlines()
    relevant = [
        index for index, line in enumerate(lines)
        if re.search(r"(?i)error|fail(?:ed|ure)?|exception|timed?\s*out|fatal", line)
    ]
    selected = set(range(max(0, len(lines) - 15), len(lines)))
    for index in relevant[-20:]:
        selected.update(range(max(0, index - 2), min(len(lines), index + 3)))
    return "\n".join(lines[index][:600] for index in sorted(selected))[-8000:]
