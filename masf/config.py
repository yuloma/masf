import os
import re
import tomllib
from pathlib import Path


ROLES = ("plan", "build", "review")


class ConfigError(ValueError):
    pass


def xdg(key, default):
    return (
        Path(os.environ.get("XDG_" + key + "_HOME") or Path.home() / default) / "masf"
    )


def required(ok, field, detail):
    if not ok:
        raise ConfigError(f"Configuración inválida: {field}: {detail}")


def table(value, field):
    required(isinstance(value, dict), field, "se requiere una tabla")
    return value


def string(value, field):
    required(isinstance(value, str) and value, field, "se requiere texto no vacío")


def array(value, field):
    required(isinstance(value, list), field, "se requiere una lista")
    return value


def routing(value, field):
    routes = table(value, field)
    for role, name in routes.items():
        route = f"{field}.{role}"
        required(role in ROLES, route, "rol desconocido")
        string(name, route)
    return routes


def compose_services(directory, item, field):
    import yaml

    file = directory / item
    try:
        resolved = file.resolve()
        allowed = (
            not Path(item).is_absolute()
            and resolved.is_relative_to(directory.resolve())
            and file.absolute() == resolved
            and file.is_file()
        )
    except (OSError, RuntimeError, ValueError):
        allowed = False
    required(
        allowed,
        field,
        f"se requiere un archivo relativo dentro de la configuración: {item}",
    )

    try:
        content = file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(
            f"Configuración inválida: {field}: no se pudo leer el archivo YAML: {item}"
        ) from exc
    try:
        data = yaml.safe_load(content)
    except (yaml.YAMLError, ValueError, LookupError, AttributeError, RecursionError) as exc:
        # PyYAML constructors can raise built-in errors for invalid tagged scalars.
        mark = getattr(exc, "problem_mark", None)
        location = f" (línea {mark.line + 1}, columna {mark.column + 1})" if mark else ""
        raise ConfigError(
            f"Configuración inválida: {field}: YAML inválido en {item}{location}"
        ) from exc

    if data is None:
        return {}
    data = table(data, field)
    field += ".services"
    services = table(data.get("services", {}), field)
    for name, definition in services.items():
        string(name, field)
        table(definition, f"{field}.{name}")
    return services


def validate_compose(profile, directory, field):
    profile = table(profile, field)
    files_field = field + ".files"
    files = array(profile.get("files"), files_field)
    required(files, files_field, "se requiere al menos un archivo")
    service_field = field + ".service"
    service = profile.get("service")
    string(service, service_field)
    required(
        re.fullmatch(r"[\w][\w.-]*", service),
        service_field,
        "nombre de servicio inválido",
    )

    services = set()
    for index, item in enumerate(files):
        item_field = f"{files_field}[{index}]"
        string(item, item_field)
        services.update(compose_services(directory, item, item_field))
    required(
        service in services,
        service_field,
        "el servicio no figura en los archivos del perfil",
    )


def load(path):
    try:
        with path.open("rb") as source:
            cfg = tomllib.load(source)
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise ConfigError(
            f"Configuración inválida: TOML: no se pudo interpretar el archivo ({exc})"
        ) from exc
    except OSError as exc:
        raise ConfigError("Configuración inválida: TOML: no se pudo leer el archivo") from exc

    version = cfg.get("version")
    required(type(version) is int and version == 1, "version", "se requiere el entero 1")
    service = table(cfg.get("service"), "service")
    for key in (
        "poll_seconds",
        "step_timeout_seconds",
        "max_build_rounds",
        "max_parallel_jobs",
    ):
        n = service.get(key)
        required(
            type(n) is int and n > 0, f"service.{key}", "se requiere un entero positivo"
        )

    for key, default in {"max_ci_technical_retries": 2, "ci_retry_seconds": 60}.items():
        n = service.setdefault(key, default)
        required(type(n) is int and n >= 0, f"service.{key}", "se requiere un entero no negativo")

    runner = table(cfg.get("runner"), "runner")
    string(runner.get("image"), "runner.image")
    providers = table(cfg.get("providers"), "providers")
    for name, provider in providers.items():
        field = f"providers.{name}"
        required(name in {"codex", "cursor", "claude"}, field, "proveedor desconocido")
        provider = table(provider, field)
        required(
            type(provider.get("enabled")) is bool, field + ".enabled", "se requiere un booleano"
        )
        string(provider.get("executable"), field + ".executable")
        if "model" in provider:
            required(
                isinstance(provider["model"], str), field + ".model", "se requiere texto"
            )

    global_routes = routing(cfg.get("routing"), "routing")
    seen = set()
    for index, repo in enumerate(array(cfg.get("repositories"), "repositories")):
        entry = f"repositories[{index}]"
        repo = table(repo, entry)
        slug = repo.get("slug")
        required(
            isinstance(slug, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", slug),
            entry + ".slug",
            "se requiere un identificador con formato propietario/repositorio",
        )
        required(
            slug.lower() not in seen, entry + ".slug", f"el repositorio {slug} está repetido"
        )
        seen.add(slug.lower())
        field = f"repositories[{slug}]"
        validate_repo(repo, field, global_routes, providers, runner["image"], path.parent)
    return cfg


def validate_repo(repo, field, global_routes, providers, runner_image, directory):
    required(
        type(repo.get("enabled")) is bool, field + ".enabled", "se requiere un booleano"
    )
    string(repo.get("label"), field + ".label")
    if "base_branch" in repo:
        required(
            isinstance(repo["base_branch"], str), field + ".base_branch", "se requiere texto"
        )
    string(repo.setdefault("runner_image", runner_image), field + ".runner_image")

    local_routes = routing(repo.get("routing", {}), field + ".routing")
    repo["routing"] = global_routes | local_routes
    for role in ROLES:
        route = f"{field}.routing.{role}"
        name = repo["routing"].get(role)
        required(name is not None, route, "falta asignar un proveedor")
        source = f" (asignación en routing.{role})" if role not in local_routes else ""
        required(
            name in providers, route, "el proveedor asignado no está configurado" + source
        )
        required(
            not repo["enabled"] or providers[name]["enabled"],
            route,
            "el proveedor asignado está deshabilitado" + source,
        )

    for key in ("setup", "checks"):
        commands_field = f"{field}.{key}"
        commands = array(repo.get(key), commands_field)
        if key == "checks":
            required(commands, commands_field, "se requiere al menos un comando")
        for index, command in enumerate(commands):
            command_field = f"{commands_field}[{index}]"
            array(command, command_field)
            required(command, command_field, "se requiere al menos un argumento")
            for arg_index, arg in enumerate(command):
                string(arg, f"{command_field}[{arg_index}]")

    paths_field = field + ".protected_paths"
    for index, value in enumerate(array(repo.get("protected_paths", []), paths_field)):
        string(value, f"{paths_field}[{index}]")
    if "compose" in repo:
        validate_compose(repo["compose"], directory, field + ".compose")
