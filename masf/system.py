import os
import re
import subprocess
import signal
import time
import tempfile
from contextlib import ExitStack


def clean(value):
    value = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        "[redacted]", value,
    )
    value = re.sub(
        r'''(?ix)
        ( (?:bearer|basic)\s+
        | \b (?: access[_-]?token | refresh[_-]?token | (?:api|private)[_-]?key
          | password | passwd | (?:github|gh|npm)?[_-]?token | secret ) ["']?\s*[:=]\s*
        )
        (?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s&,;"'<>]+)
        ''',
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"\b(?:gh[pousr]_|github_pat_|sk-|AKIA|ASIA)[A-Za-z0-9_-]+",
        "[redacted]", value,
    )
    return re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[redacted]@", value)


class ProcessError(RuntimeError):
    def __init__(self, message, stdout="", stderr=""):
        super().__init__(message)
        self.stdout = clean(stdout)
        self.stderr = clean(stderr or "")


class TaskStopped(RuntimeError):
    pass


def command(
    args, *, cwd=None, stdin=None, env=None, timeout=120, combined=False, guard=None
):
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        source = None
        if stdin is not None:
            source = stack.enter_context(
                tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            )
            source.write(stdin)
            source.seek(0)
        proc = stack.enter_context(
            subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if combined else subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
            )
        )
        try:
            while True:
                if guard:
                    guard()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"{args[0]}: tiempo agotado ({timeout}s)")
                try:
                    stdout, stderr = proc.communicate(timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        if proc.returncode:
            raise ProcessError(
                f"{args[0]} ({proc.returncode}): " + clean(stderr or stdout)[-1800:],
                stdout,
                stderr,
            )
        return stdout.rstrip("\n")


def docker_env():
    return {
        key: os.environ[key]
        for key in ("PATH", "DOCKER_HOST", "HOME")
        if key in os.environ
    }
