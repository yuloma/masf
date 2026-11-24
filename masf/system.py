import os
import re
import subprocess
import signal
import time


def clean(value):
    value = re.sub(
        r'(?i)(bearer\s+|(?:access_token|refresh_token|api_key|password)["\s:=]+)\S+',
        r"\1[redacted]",
        value,
    )
    return re.sub(r"(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+", "[redacted]", value)


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
    with subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if combined else subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            while True:
                if guard:
                    guard()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"{args[0]}: tiempo agotado ({timeout}s)")
                try:
                    stdout, stderr = proc.communicate(
                        input=stdin, timeout=min(1, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    stdin = None
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
