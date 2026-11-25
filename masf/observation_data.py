"""Public projections for the local observation API."""

import math
import re
from urllib.parse import urlsplit, urlunsplit

from .evidence import historical_role
from .github_ci import filter_log
from .queue import revision
from .system import clean


def mapping(value):
    return value if isinstance(value, dict) else {}


def items(value):
    return value if isinstance(value, list) else []


def scalar(value):
    if isinstance(value, str):
        return clean(value)[:8000]
    if type(value) in (bool, int) or type(value) is float and math.isfinite(value):
        return value
    return None


def fields(value, names):
    value = mapping(value)
    return {name: scalar(value.get(name)) for name in names.split()}


def strings(value):
    return [scalar(item) for item in items(value) if isinstance(item, str)]


def protected_paths(reason):
    # Historical blocks record only the first conflicting path.
    prefix = "Ruta protegida: "
    if isinstance(reason, str) and reason.startswith(prefix) and reason[len(prefix):]:
        return [scalar(reason[len(prefix):])]
    return []


def build_checkpoint(job):
    checkpoint = mapping(job.get("build_workspace"))
    if not checkpoint:
        return None
    sha = job.get("candidate") or job.get("base")
    fingerprint = job.get("fingerprint")
    return fields(checkpoint, "input_sha round failed_attempt") | {
        "matches_input": checkpoint["input_sha"] == sha if checkpoint.get("input_sha") and sha else None,
        "matches_scope": checkpoint["fingerprint"] == fingerprint
        if checkpoint.get("fingerprint") and fingerprint else None,
    }


def url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme == "https" and parts.netloc in {"github.com", "api.github.com"}:
        return urlunsplit(("https", parts.netloc, clean(parts.path), "", ""))
    return None


def links(value, names):
    return {name: url(mapping(value).get(name)) for name in names.split()}


def check_report(value):
    if not mapping(value):
        return None
    commands = [fields(item, "code seconds") for item in items(value.get("commands"))]
    return fields(value, "sha passed failure_kind retryable stage_attempt service profile_sha256") | {
        "commands": commands,
    }


def model(value, *, active=False, applicable=True):
    public = fields(value, "requested effective source availability")
    if not applicable:
        public["availability"] = "not_applicable"
    elif public["availability"] != "reported":
        public["availability"] = "pending" if active else "unknown"
    return public


def usage(value, *, active=False, applicable=True):
    value = mapping(value)
    if not applicable:
        availability = "not_applicable"
    elif value.get("availability") == "reported":
        availability = "reported"
    else:
        availability = "pending" if active else "unknown"
    return {"availability": availability, "measurements": [
        fields(item, "name value unit source kind") for item in items(value.get("measurements"))
    ]}


def explanation(value, *, active=False):
    value = mapping(value)
    public = fields(value, "summary reason source")
    return public | {
        "availability": "available" if value.get("availability") == "available"
        else "pending" if active else "unknown",
        "decisions": strings(value.get("decisions")),
        "criteria": strings(value.get("criteria")),
        "evidence": [],
    }


def stage_result(role, value):
    if not mapping(value):
        return None
    if role == "checks":
        return check_report(value)
    return fields(value, "version summary question verdict confirmed pr sha") | {
        key: strings(value.get(key)) for key in ("criteria", "changes", "findings")
    }


def stage(value):
    active = value.get("status") == "active"
    applicable = value.get("role") in {"plan", "build", "review"} or bool(value.get("provider"))
    public = fields(value, "id role executor provider execution_id execution_id_source cli_version "
                    "input_sha candidate_sha started_at finished_at detected_at duration_seconds "
                    "status error error_type purpose previous_attempt retry_of pr legacy_model configured_timeout_seconds")
    public["historical"] = value.get("historical") is True
    public["identity_availability"] = (
        "available" if public["executor"] else "pending" if active else "unknown"
    )
    for key in ("execution_id", "cli_version"):
        public[key + "_availability"] = (
            "available" if public[key] is not None else "not_applicable" if not applicable
            else "pending" if active else "unknown"
        )
    public["model"] = model(value.get("model"), active=active, applicable=applicable)
    if value.get("role") in {"checks", "publish"}:
        commands = [argv[0] for argv in items(value.get("commands")) if items(argv)]
        public["executables"] = [scalar(name) for name in commands
                                 if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.+-]+", name)]
    if value.get("role") == "publish":
        public["operations"] = [
            " ".join(argv[:2]) for argv in items(value.get("commands")) if len(items(argv)) >= 2
            and argv[:2] in (["git", "push"], ["git", "ls-remote"], ["gh", "api"])
        ]
    if value.get("error_type") == "CommandTimeout":
        match = re.search(r"tiempo agotado \((\d{1,9})s\)$", public["error"] or "")
        public["timeout_seconds"] = int(match[1]) if match else None
    public["usage"] = usage(value.get("usage"), active=active, applicable=applicable)
    public["explanation"] = explanation(value.get("explanation"), active=active)
    public["provenance"] = fields(value.get("provenance"), "engine policy")
    public["result"] = stage_result(value.get("role"), value.get("result"))
    for key, names in (("ci_origin", "id pr sha"), ("ci_result", "pr sha")):
        public[key] = fields(value[key], names) if mapping(value.get(key)) else None
    return public


def historical_stage(role, record, ident):
    if mapping(record) and not record.get("stage_attempt"):
        return historical_role(role, record) | {
            "id": ident, "evidence_file": record.get("evidence_file"),
        }
    return None


def operation(value):
    entry = mapping(value.get("decision"))
    executor = scalar(value.get("executor"))
    return {
        "role": "operation", "executor": executor, "provider": None,
        "identity_availability": "available" if executor else "unknown",
        "execution_id": None, "execution_id_source": None, "cli_version": None,
        "execution_id_availability": "not_applicable", "cli_version_availability": "not_applicable",
        "at": scalar(value.get("at")), "sha": scalar(value.get("sha")),
        "model": model(None, applicable=False), "usage": usage(None, applicable=False),
        "decision": fields(entry, "id kind operator at evidence pr head candidate base rounds "
                           "result outcome status reason resume_state role provider previous_provider round_limit "
                           "timeout_seconds previous_timeout_seconds workspace") | {"paths": strings(entry.get("paths"))},
        "explanation": explanation(value.get("explanation")),
    }


def observation_error(value):
    if isinstance(value, str):
        return {"message": scalar(value), "status": None}
    return fields(value, "message status")


def error_map(value):
    return {clean(key): observation_error(error) for key, error in mapping(value).items()}


def diagnostic(value):
    if not mapping(value):
        return None
    return fields(value, "kind evidence observation_key") | {
        "check": check(value["check"]) if mapping(value.get("check")) else None,
        "output": fields(value.get("output"), "title summary text"),
        "annotations": [
            fields(item, "path start_line end_line annotation_level title message raw_details")
            for item in items(value.get("annotations"))
        ],
        "log_excerpt": filter_log(value["log_excerpt"])[-8000:]
        if isinstance(value.get("log_excerpt"), str) else None,
        "sources": [ref for item in strings(value.get("sources"))
                    if (ref := url("https://api.github.com/" + item if item.startswith("repos/") else item))],
        "errors": [fields(item, "source error") for item in items(value.get("errors"))],
    } | links(value, "annotations_url log_url details_url")


def check(value):
    value = mapping(value)
    return fields(value, "id key name source status github_state phase conclusion current "
                  "check_id run run_id workflow_id job_id attempt app_id suite_id created_at "
                  "updated_at started_at completed_at run_started_at description") | links(
        value, "url html_url details_url target_url job_url run_url check_run_url"
    ) | {
        "steps": strings(value.get("steps")),
        "output": fields(value.get("output"), "title annotations_count"),
        "diagnostic": diagnostic(value.get("diagnostic")),
    }


def repair(value):
    if not mapping(value):
        return None
    return fields(value, "id sha pr decision") | {
        "limits": fields(value.get("counters"), "rounds functional build_limit technical technical_limit"),
        "diagnostics": [diagnostic(item) for item in items(value.get("diagnostics"))],
    }


def feedback(value):
    if not mapping(value):
        return scalar(value)
    return fields(value, "source sha type summary question verdict reason instruction") | {
        "findings": strings(value.get("findings")),
        "paths": strings(value.get("paths")),
        "checks": check_report(value) if "commands" in value else None,
        "diagnostics": [diagnostic(item) for item in items(value.get("diagnostics"))],
    }


def local_ci(value):
    if not mapping(value):
        return None
    return fields(value, "sha state") | {
        "checks": [check(item) for item in items(value.get("checks"))],
        "workflows": [workflow(item) for item in items(value.get("workflows"))],
    }


def workflow(value):
    return fields(value, "id workflow_id check_suite_id name run_number run_attempt status conclusion "
                  "created_at updated_at run_started_at") | links(value, "html_url url jobs_url")


def snapshot(value):
    return fields(value, "base_ref observed_at") | {
        "checks": {clean(key): check(item) for key, item in mapping(value.get("checks")).items()},
        "runs": {
            clean(key): fields(item, "run_id workflow_id attempt historical current phase jobs_complete")
            | links(item, "workflow_url url attempt_url log_url")
            | {"run": workflow(item["run"]) if mapping(item.get("run")) else None,
               "jobs": {clean(ident): check(job) for ident, job in mapping(item.get("jobs")).items()}}
            for key, item in mapping(value.get("runs")).items() if mapping(item)
        },
        "required": fields(value.get("required"), "known") | {
            key: [fields(entry, "name app_id") for entry in entries]
            if isinstance(entries := mapping(value.get("required")).get(key), list) else None
            for key in ("checks", "missing", "unverified")
        },
        "errors": error_map(value.get("errors")),
        "last_success_at": fields(value.get("last_success_at"),
                                  "check_runs commit_statuses workflow_runs branch_protection branch_rules"),
    }


def pull_ref(value):
    return fields(value, "repo_id repo ref sha") if mapping(value) else None


def pull_provenance(value):
    return fields(value, "kind candidate head_sha base_ref base_sha merged_sha source") | {
        "parents": strings(mapping(value).get("parents")),
    }


def pull(value):
    return fields(value, "repo_id slug number id state draft observed_at") | links(value, "url") | {
        "base": pull_ref(value.get("base")), "head": pull_ref(value.get("head")),
        "job": fields(value["job"], "number candidate rounds method") if mapping(value.get("job")) else None,
        "provenance": pull_provenance(value.get("provenance")),
        "errors": error_map(value.get("errors")),
    }


def pull_detail(kind, value):
    if kind == "pr_discovered":
        return pull(value)
    projections = {
        "pr_head": pull_ref, "pr_base": pull_ref, "pr_provenance": pull_provenance,
        "pr_state": scalar, "pr_draft": scalar,
        "pr_job": lambda item: fields(item, "number candidate rounds method")
        if mapping(item) else scalar(item),
    }
    if kind in projections:
        detail = {key: projections[kind](value.get(key)) for key in ("before", "after")}
        if kind == "pr_job":
            detail["method"] = scalar(value.get("method"))
            detail["operations"] = [operation(item) for item in items(value.get("operations")) if mapping(item)]
        return detail
    if kind == "required_checks":
        return snapshot({"required": value})["required"]
    if kind in {"observation_error", "observation_recovered"}:
        return fields(value, "source") | {
            "error": observation_error(value["error"]) if value.get("error") is not None else None,
        }
    if kind == "pr_linked":
        return fields(value, "operator number")
    return check(value) | fields(value, "previous_key inferred")
