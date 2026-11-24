"""Run the real CLI with local test adapters and deterministic crash barriers."""

import json
import os
import sys
import threading
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from masf import checks, cli, providers, queue, worker
from masf.system import command

root = Path(os.environ["CRASH_ROOT"])
crash_at = os.environ["CRASH_POINT"]
github = os.environ["CRASH_GITHUB"]
use_engine = os.environ["CRASH_ENGINE"] == "1"

real_claim = cli.claim
real_git = worker.Worker.git
real_verify = checks.verify


def checkpoint(point):
    if point == crash_at:
        (root / point).touch()
        threading.Event().wait(90)
        raise RuntimeError("Harness did not kill the controller at " + point)


def claim(*args):
    claimed = real_claim(*args)
    if claimed:
        checkpoint("reserved")
    return claimed


def git(self, *args, **kwargs):
    result = real_git(self, *args, **kwargs)
    if args[0] == "push":
        checkpoint("pushed")
    return result


def local_command(args, **kwargs):
    if args[:2] == ["gh", "api"]:
        include = args[2] == "--include"
        endpoint = args[3] if include else args[2]
        data = kwargs["stdin"].encode() if args[-2:] == ["--input", "-"] else None
        request = Request(github + "/" + endpoint, data=data)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            body = response.read().decode()
            headers = f"HTTP/1.1 {response.status}\r\n{response.headers}\r\n"
        if data is not None:
            checkpoint("created")
        return headers + body if include else body
    if not use_engine and args[0] == "docker":
        assert args[:4] == ["docker", "ps", "-aq", "--filter"], args
        assert len(args) == 5 and args[4].startswith("label=masf.owner="), args
        return ""
    output = command(args, **kwargs)
    if args[:2] == ["docker", "compose"] and "down" in args:
        append("cleanup.jsonl", [args, kwargs["env"]["MASF_WORKSPACE"]])
    return output


def append(name, value):
    with (root / name).open("a") as file:
        file.write(json.dumps(value) + "\n")


def invoke(role, job, cfg, workspace, inputs, owner, *, guard=None):
    sha = inputs["sha"]
    append("calls.jsonl", [role, sha])
    if role == "plan":
        result = {"criteria": ["AC1 Add the greeting"]}
    elif role == "build":
        (workspace / "greeting").write_text("Hello\n")
        result = {"changes": ["AC1 Added the greeting"]}
    else:
        assert role == "review", role
        assert "+Hello" in inputs["diff"], inputs["diff"]
        assert inputs["checks"]["passed"] and inputs["checks"]["sha"] == sha
        result = {"verdict": "pass", "summary": "Greeting reviewed"}
    return {"result": result, "provider": job["repo"]["routing"][role]}


def verify(repo, config, workspace, sha, folder, owner, timeout, *, guard=None):
    append("calls.jsonl", ["check", sha])
    if use_engine:
        return real_verify(
            repo, config, workspace, sha, folder, owner, timeout, guard=guard
        )
    (argv,) = repo["checks"]
    output = command(argv, cwd=workspace, timeout=10, guard=guard)
    return {
        "sha": sha,
        "passed": True,
        "commands": [{"argv": argv, "code": 0, "log": output}],
    }


if __name__ == "__main__":
    cli.claim = claim
    worker.Worker.git = git
    queue.command = worker.command = local_command
    providers.invoke = invoke
    checks.verify = verify
    sys.exit(cli.main())
