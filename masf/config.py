import os
import re
import tomllib
from pathlib import Path


def xdg(key, default):
    return (
        Path(os.environ.get("XDG_" + key + "_HOME") or Path.home() / default) / "masf"
    )


def required(ok, field):
    if not ok:
        raise ValueError(f"Configuración inválida: {field}")


def strings(value):
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def load(path):
    with path.open("rb") as source:
        cfg = tomllib.load(source)
    required(cfg.get("version") == 1, "version")
    for key in (
        "poll_seconds",
        "step_timeout_seconds",
        "max_build_rounds",
        "max_parallel_jobs",
    ):
        n = cfg["service"].get(key)
        required(type(n) is int and n > 0, "service." + key)
    required(
        isinstance(cfg["runner"].get("image"), str) and cfg["runner"]["image"],
        "runner.image",
    )
    seen = set()
    for name, provider in cfg["providers"].items():
        required(name in {"codex", "cursor", "claude"}, "providers." + name)
        required(
            type(provider.get("enabled")) is bool and provider.get("executable"), name
        )
    for repo in cfg["repositories"]:
        slug = repo["slug"]
        required(
            re.fullmatch(r"[\w.-]+/[\w.-]+", slug) and slug.lower() not in seen, slug
        )
        seen.add(slug.lower())
        required(
            type(repo.get("enabled")) is bool
            and isinstance(repo.get("label"), str)
            and repo["label"],
            slug + ".label/enabled",
        )
        repo["routing"] = cfg["routing"] | repo.get("routing", {})
        repo.setdefault("runner_image", cfg["runner"]["image"])
        required(set(repo["routing"]) == {"plan", "build", "review"}, slug + ".routing")
        for role, name in repo["routing"].items():
            required(
                name in cfg["providers"]
                and (not repo["enabled"] or cfg["providers"][name]["enabled"]),
                f"{slug}.routing.{role}",
            )
        for key in ("setup", "checks"):
            commands = repo.get(key)
            required(
                isinstance(commands, list)
                and all(strings(cmd) and cmd for cmd in commands),
                f"{slug}.{key}",
            )
        required(
            repo["checks"] and strings(repo.get("protected_paths", [])),
            slug + ".checks/protected_paths",
        )
        if "compose" in repo:
            profile = repo["compose"]
            required(
                strings(profile.get("files")) and profile["files"],
                slug + ".compose.files",
            )
            required(
                re.fullmatch(r"[\w][\w.-]*", profile.get("service", "")),
                slug + ".compose.service",
            )
            import yaml

            services = set()
            for item in profile["files"]:
                file = path.parent / item
                required(
                    not Path(item).is_absolute()
                    and file.resolve().is_relative_to(path.parent.resolve())
                    and file.is_file()
                    and file.absolute() == file.resolve(),
                    slug + ".compose.files: " + item,
                )
                data = yaml.safe_load(file.read_text()) or {}
                services.update(data.get("services", {}))
            required(profile["service"] in services, slug + ".compose.service")
    return cfg
