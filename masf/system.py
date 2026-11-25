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
        | --(?:[\w-]*-)?(?:password|passwd|token|secret|api[-_]?key)\s+
        | \b (?: access[_-]?token | refresh[_-]?token | (?:api|private)[_-]?key
          | password | passwd | signature | credential | [\w-]*[_-](?:token|secret|password|signature|credential|api[_-]?key)
          | (?:github|gh|npm)?[_-]?token | secret ) ["']?\s*[:=]\s*
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


_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:token|secret|password|passwd|credential|signature|(?:api|private)[_-]?key)$",
    re.I,
)


def secret_key(key):
    return bool(_SECRET_KEY.search(re.sub(r"([a-z])([A-Z])", r"\1_\2", key)))


def filtered(value):
    """Filter strings before serialization so multiline values cannot corrupt JSON."""
    if isinstance(value, dict):
        return {clean(key): "[redacted]" if secret_key(key) and item is not None else filtered(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return ["[redacted]" if index and isinstance(value[index - 1], str)
                and value[index - 1].startswith("-") and secret_key(value[index - 1].lstrip("-"))
                else filtered(item) for index, item in enumerate(value)]
    return clean(value) if isinstance(value, str) else value


class ProcessError(RuntimeError):
    def __init__(self, message, stdout="", stderr="", returncode=None):
        super().__init__(message)
        self.stdout = clean(stdout)
        self.stderr = clean(stderr or "")
        self.returncode = returncode


class TaskStopped(RuntimeError):
    pass


class CommandTimeout(RuntimeError):
    pass


def command(
    args,
    *,
    cwd=None,
    stdin=None,
    env=None,
    timeout=120,
    combined=False,
    guard=None,
    on_stdout_line=None,
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
        output_file = error_file = reader = None
        if on_stdout_line:
            output_file = stack.enter_context(
                tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8")
            )
            reader = stack.enter_context(
                open(output_file.name, encoding="utf-8", errors="replace")
            )
            if not combined:
                error_file = stack.enter_context(
                    tempfile.TemporaryFile(mode="w+", encoding="utf-8")
                )
        pending = ""

        def drain(final=False):
            nonlocal pending
            if reader:
                pending += reader.read()
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    if line:
                        on_stdout_line(line)
                if final and pending:
                    on_stdout_line(pending)
                    pending = ""

        if guard:
            guard()
        proc = stack.enter_context(
            subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=source,
                stdout=output_file or subprocess.PIPE,
                stderr=subprocess.STDOUT
                if combined
                else (error_file or subprocess.PIPE),
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
                    raise CommandTimeout(f"{args[0]}: tiempo agotado ({timeout}s)")
                try:
                    if on_stdout_line:
                        proc.wait(timeout=min(1, remaining))
                        drain(final=True)
                        output_file.seek(0)
                        stdout = output_file.read()
                        if error_file:
                            error_file.seek(0)
                        stderr = error_file.read() if error_file else None
                    else:
                        stdout, stderr = proc.communicate(timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    drain()
                    continue
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            drain(final=True)
            raise
        if proc.returncode:
            detail = stderr or (
                "Consulta la actividad del proveedor" if on_stdout_line else stdout
            )
            raise ProcessError(
                f"{args[0]} ({proc.returncode}): " + clean(detail)[-1800:],
                "" if on_stdout_line else stdout,
                stderr,
                proc.returncode,
            )
        return stdout.rstrip("\n")


def docker_env():
    return {
        key: os.environ[key]
        for key in ("PATH", "DOCKER_HOST", "HOME")
        if key in os.environ
    }
