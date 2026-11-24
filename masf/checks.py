import hashlib
import json
import os
import re
import time
import uuid

from .system import TaskStopped, clean, command, docker_env


def allow_keys(data, keys, field):
    extra = set(data or {}) - set(keys.split())
    if extra:
        raise ValueError(f"Perfil {field}: campos no admitidos: {sorted(extra)}")


def compose_files(config, profile, folder, workspace):
    import yaml

    target = []
    for index, item in enumerate(profile["files"]):
        path = config.parent / item
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(
            config.parent.resolve()
        ):
            raise ValueError("Ruta de perfil fuera de la configuración")
        raw = path.read_text()
        doc = yaml.safe_load(raw)
        allow_keys(doc, "services networks volumes", item)
        for svc in doc.get("services", {}).values():
            allow_keys(
                svc,
                "image user command working_dir depends_on environment volumes healthcheck networks entrypoint build ports",
                item,
            )
            if "build" in svc and svc["build"] not in (
                "${MASF_WORKSPACE}",
                {"context": "${MASF_WORKSPACE}"},
            ):
                raise ValueError("build.context requiere MASF_WORKSPACE")
            if "build" in svc and "image" in svc:
                raise ValueError("build.image debe tener nombre por proyecto")
            for mount in svc.get("volumes", []):
                if isinstance(mount, str):
                    if not re.fullmatch(
                        r"[\w-]+:/[\w/.-]+(:ro)?", mount
                    ) or mount.split(":")[0] not in doc.get("volumes", {}):
                        raise ValueError("Volumen no permitido: " + mount)
                else:
                    allow_keys(mount, "type source target read_only bind", "mount")
                    if mount["type"] != "bind" or not re.fullmatch(
                        r"\$\{MASF_WORKSPACE(?::\?[^}]*)?\}", mount["source"]
                    ):
                        raise ValueError("Montaje fuera del checkout")
                    allow_keys(mount.get("bind"), "create_host_path", "bind")
            if any(not isinstance(p, int) for p in svc.get("ports", [])):
                raise ValueError("Solo se permiten puertos dinámicos")
        for network in (doc.get("networks") or {}).values():
            allow_keys(network, "internal ipam", "network")
            if network and "ipam" in network:
                allow_keys(network["ipam"], "config", "ipam")
                for subnet in network["ipam"]["config"]:
                    allow_keys(subnet, "subnet", "subnet")
        for volume in (doc.get("volumes") or {}).values():
            allow_keys(volume, "", "volume")
        out = folder / f"profile-{index}.yaml"
        out.write_text(raw)
        target.extend(["-f", str(out)])
    return target


def verify(repo, config, workspace, sha, folder, owner, timeout, *, guard=None):
    deadline = time.monotonic() + timeout

    def remaining():
        return max(0, deadline - time.monotonic())

    name = "masf-check-" + uuid.uuid4().hex
    env = docker_env() | {"MASF_WORKSPACE": str(workspace)}
    record = {"sha": sha, "name": name, "commands": [], "passed": False}
    folder.mkdir()
    report = folder / "checks.json"
    file = folder / "cleanup.json"
    compose = repo.get("compose")
    if compose:
        profiles = compose_files(config, compose, folder, workspace)
        stack = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-directory",
            str(folder),
            "-p",
            name,
            *profiles,
        ]
        model = json.loads(
            command(
                stack + ["config", "--format", "json"],
                env=env,
                timeout=remaining(),
                guard=guard,
            )
        )
        record["profile_sha256"] = hashlib.sha256(
            json.dumps(model, sort_keys=True).encode()
        ).hexdigest()
        record["service"] = compose["service"]
        for svc in model["services"].values():
            if "build" in svc and (
                "image" in svc or svc["build"]["context"] != str(workspace)
            ):
                raise ValueError("La imagen construida debe ser exclusiva del proyecto")
            for mount in svc.get("volumes", []):
                if mount["type"] == "bind" and mount["source"] != str(workspace):
                    raise ValueError("Montaje fuera del checkout")
        if compose["service"] not in model["services"]:
            raise ValueError("No existe el servicio " + compose["service"])
        start = stack + [
            "up",
            "-d",
            "--build",
            "--wait",
            "--wait-timeout",
            str(timeout),
        ]
        execute = stack + ["exec", "-T", compose["service"]]
        stop = stack + ["down", "--volumes", "--remove-orphans"]
        if any("build" in svc for svc in model["services"].values()):
            # Builds have no explicit image name, so only their project tags are local.
            stop.extend(["--rmi", "local"])
    else:
        start = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "masf.owner=" + owner,
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-e",
            "HOME=/tmp",
            "-v",
            f"{workspace}:/workspace",
            "-w",
            "/workspace",
            repo["runner_image"],
            "sleep",
            "infinity",
        ]
        execute = ["docker", "exec", name]
        stop = ["docker", "rm", "-f", name]
    report.write_text(json.dumps(record, indent=2))
    file.write_text(json.dumps([stop, str(workspace)]))
    try:
        command(start, env=env, timeout=remaining(), guard=guard)
        for args in repo["setup"] + repo["checks"]:
            then = time.monotonic()
            entry = {"argv": args}
            record["commands"].append(entry)
            try:
                output = command(
                    execute + args,
                    env=env,
                    timeout=remaining(),
                    combined=True,
                    guard=guard,
                )
                entry.update(code=0, log=clean(output)[-4000:])
            except TaskStopped:
                record["failure_kind"] = "cancelled"
                raise
            except RuntimeError as exc:
                error = str(exc)
                code = re.match(r"docker \((\d+)\)", error)
                entry.update(code=int(code[1]) if code else None, log=error)
                record["failure_kind"] = (
                    "infrastructure"
                    if entry["code"] in (None, 125, 126, 127)
                    else "functional"
                )
                break
            finally:
                entry["seconds"] = round(time.monotonic() - then, 3)
        else:
            record["passed"] = True
    finally:
        report.write_text(json.dumps(record, indent=2))
        command(stop, env=env, timeout=timeout)
        file.unlink()
    return record
