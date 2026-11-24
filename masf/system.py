import os
import re
import subprocess


def clean(value):
    value = re.sub(
        r'(?i)(bearer\s+|(?:access_token|refresh_token|api_key|password)["\s:=]+)\S+',
        r"\1[redacted]",
        value,
    )
    return re.sub(r"(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+", "[redacted]", value)


def command(args, *, cwd=None, stdin=None, env=None, timeout=120, combined=False):
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            input=stdin,
            env=env,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if combined else subprocess.PIPE,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{args[0]}: tiempo agotado ({timeout}s)") from None
    if proc.returncode:
        raise RuntimeError(
            f"{args[0]} ({proc.returncode}): "
            + clean(proc.stderr or proc.stdout)[-1800:]
        )
    return proc.stdout.rstrip("\n")


def docker_env():
    return {
        key: os.environ[key]
        for key in ("PATH", "DOCKER_HOST", "HOME")
        if key in os.environ
    }
