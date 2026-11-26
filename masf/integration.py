"""Independent review and guarded pull request integration."""

import fnmatch
import hashlib
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from . import ci, github_ci
from .config import MERGE_PROOFS, load
from .evidence import digest, revisions, timestamp
from .queue import api, eligible, http_status, require_lease, retry_delay, save
from .system import TaskStopped, filtered


STATE = "merge_review"
SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
RISKS = ("low", "elevated", "critical")
CRITICAL_PATHS = (
    ".github/**", "Dockerfile*", "**/Dockerfile*", "**/compose*.y*ml",
    "compose*.y*ml", "docker-compose*.y*ml", "**/docker-compose*.y*ml",
    "deploy/**", "**/deploy/**", "infra/**", "**/infra/**", "**/*.tf", "*.tf",
    "migrations/**", "**/migrations/**", "secrets/**", "**/secrets/**",
    "AGENTS.md", "**/AGENTS.md", ".codex/**", ".cursor/**", ".claude/**",
)
RESULT_LISTS = ("reasons", "risks", "affected", "evidence", "missing_tests", "blocking_findings", "proofs")
RESULT_SHAPE = (
    '{"version":1,"summary":"...","decision":"auto_merge or human_required",'
    '"base":"SHA","head":"SHA","risk":"low, elevated or critical","reasons":["..."],'
    '"risks":[],"affected":["..."],"evidence":["CI or local evidence ID"],'
    '"missing_tests":[],"blocking_findings":[],"proofs":[],"additional_verification":false}'
)
INTEGRATION_ERRORS = (OSError, RuntimeError, ValueError, KeyError, TypeError)


def validate_result(result, binding=None):
    fields = {"version", "summary", "decision", "base", "head", "risk",
              "additional_verification", *RESULT_LISTS}
    if (not isinstance(result, dict) or set(result) != fields
            or type(result["version"]) is not int or result["version"] != 1
            or not isinstance(result["summary"], str) or not result["summary"].strip()
            or result["decision"] not in ("auto_merge", "human_required")
            or result["risk"] not in RISKS
            or type(result["additional_verification"]) is not bool):
        raise ValueError("Resultado inválido de la revisión de integración")
    for key in ("base", "head"):
        if not isinstance(result[key], str) or not SHA.fullmatch(result[key]):
            raise ValueError("SHA inválido en la revisión de integración")
        if binding and result[key] != binding[key]:
            raise ValueError("La revisión de integración evaluó otro SHA")
    for key in RESULT_LISTS:
        values = result[key]
        if (not isinstance(values, list) or len(values) > 100
                or not all(isinstance(value, str) and value.strip() and len(value) <= 4000 for value in values)):
            raise ValueError("Campo inválido en la revisión de integración: " + key)
    if not result["reasons"] or not result["affected"] or set(result["proofs"]) - MERGE_PROOFS:
        raise ValueError("Faltan motivos o componentes, o hay tipos de prueba desconocidos")
    return result


def policy_for(repo):
    return repo.get("merge_policy", {"enabled": False})


def registry_for(cfg, job):
    return next((repo for repo in cfg["repositories"] if repo["slug"] == job["slug"] and repo["enabled"]), {})


def binding_for(job, pr, repo):
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in (base, head)):
        raise ValueError("Referencias inválidas del PR")
    return {
        "repo_id": job["repo_id"], "pr": pr["number"], "base": base, "head": head,
        "base_ref": job["branch"], "head_ref": ci.branch(job), "scope": job["fingerprint"],
        "policy": digest({key: repo.get(key) for key in (
            "merge_policy", "base_branch", "label", "protected_paths", "checks", "setup", "compose", "runner_image",
            "merge_profile_digests",
        )} | {"engine_policy": revisions()["policy"]}),
    }


def references(job, lookup):
    root = f"repos/{job['slug']}/git/ref/heads/"
    return tuple(lookup(root + quote(ref, safe="/"))["object"]["sha"]
                 for ref in (job["branch"], ci.branch(job)))


def ci_evidence(snapshot):
    return {f"ci:{index}": item for index, item in enumerate(snapshot["checks"], 1)}


def schedule(db, job, pr, repo, previous_ci):
    if job.get("merge_evaluation") and job.get("ci") != previous_ci:
        save(db, job, job["state"], integration_approval=None, merge_evaluation=None)
    if (job["state"] != "pr_open" or not policy_for(repo)["enabled"]
            or (job.get("merge_observation") or {}).get("status") != "compatible"):
        return
    observation = job.get("ci") or {}
    if observation.get("state") == "success":
        save(db, job, STATE, merge_waits=0, merge_ci_waits=None, ci_retry_at=0,
             reason="Revisión independiente de integración pendiente")
    elif observation.get("state") == "waiting":
        prior = job.get("merge_ci_waits") or {}
        head = observation["sha"]
        count = prior["count"] + 1 if prior.get("head") == head else 1
        waits = {"head": head, "count": count}
        if count < 3:
            save(db, job, "pr_open", merge_ci_waits=waits)
            return
        reason = "Ejecuta los checks de CI declarados para el head y repite la revisión"
        request = {"binding": binding_for(job, pr, repo), "at": timestamp(),
                   "reasons": [reason], "next_action": "Usa resume --retry-merge-review tras obtener CI"}
        save(db, job, "needs_input", retry="pr_open", reason=reason, merge_ci_waits=waits,
             merge_intervention=request)


def blockers(result, policy, paths, remote, reports, *, incorporates_base):
    reasons = []
    critical = (*CRITICAL_PATHS, *policy["critical_paths"], *policy["human_only_paths"])
    protected = [path for path in paths if any(
        fnmatch.fnmatchcase(path.casefold(), pattern.casefold()) for pattern in critical
    )]
    if protected:
        reasons.append("Revisa las rutas reservadas a una persona: " + ", ".join(protected))
    if result["risk"] not in policy["allowed_risks"]:
        reasons.append("La política reserva esta clase de riesgo a una persona: " + result["risk"])
    if not incorporates_base:
        reasons.append("Actualiza el head para incorporar la base evaluada y repite CI")
    if result["decision"] != "auto_merge":
        reasons.extend(result["reasons"])
    reasons.extend(result["blocking_findings"])
    reasons.extend(result["missing_tests"])
    known = set(remote) | set(reports)
    cited = set(result["evidence"])
    if not cited or cited - known or not cited.intersection(reports):
        reasons.append("Identifica la evidencia verificable utilizada, incluidos los checks locales del head")
    required = set(policy["required_ci"].get(result["risk"], []))
    proofs = set(result["proofs"])
    if result["risk"] == "elevated":
        proofs.add("integration")
    for proof in sorted(proofs):
        contexts = policy["proof_checks"].get(proof)
        if contexts:
            required.update(contexts)
        else:
            reasons.append("Registra checks aprobados para la prueba de " + proof)
    observed = {item["name"] for ref, item in remote.items()
                if ref in cited and item["status"] == "completed" and item["conclusion"] == "success"}
    if required - observed:
        reasons.append("Ejecuta o acredita los checks obligatorios del head: " + ", ".join(sorted(required - observed)))
    if not paths:
        reasons.append("Comprueba el alcance del PR sin cambios")
    return reasons


def confirm(db, job, pr):
    """Record the remote outcome before ordinary PR observation can close the job."""
    intent = job.get("merge_intent")
    approval = job.get("integration_approval") or {}
    if not intent and not approval:
        return False
    if not ci.provenance(job, pr):
        return False
    binding = (intent or approval).get("binding")
    if not binding:
        return False
    if pr.get("state") == "open":
        if (intent and intent["operation"] == "ready" and pr.get("draft") is False
                and pr["head"]["sha"] == binding["head"] and pr["base"]["sha"] == binding["base"]):
            save(db, job, job["state"], merge_intent=None, merge_waits=0,
                 merge_result={"status": "ready", "binding": binding, "at": timestamp()})
        return False
    if pr.get("state") != "closed":
        return False
    merged = bool(pr.get("merged") or pr.get("merged_at"))
    status = "closed"
    if merged:
        status = "merged"
        if pr["head"]["sha"] != binding["head"]:
            status = "merged_other_head"
        elif pr["base"]["sha"] != binding["base"]:
            status = "merged_unverified_base"
    result = {
        "status": status,
        "binding": binding, "merged_sha": pr.get("merge_commit_sha") if merged else None,
        "observed_base": pr["base"]["sha"], "observed_head": pr["head"]["sha"], "at": timestamp(),
        "source": "github_observation", "attempted_operation": intent["operation"] if intent else None,
    }
    save(db, job, "merged" if merged else "closed", reason="", integration_approval=None,
         merge_intent=None, merge_result=result, merge_intervention=None)
    return True


class Review:
    def __init__(self, worker, lookup=api, *, conditional_update=None):
        self.worker, self.job, self.lookup = worker, worker.job, lookup
        # An adapter must enforce the PR identity, both refs and SHAs, and GitHub
        # rules at the remote mutation. Open/ready state supplies idempotence.
        # GitHub's head precondition alone cannot enforce the reviewed base.
        self.conditional_update = conditional_update

    def human(self, reasons, *, binding=None, evaluation=None):
        job = self.job
        prior = job.get("merge_evaluation") or {}
        binding = binding or prior.get("binding")
        request = {"binding": binding, "reasons": reasons, "at": timestamp(),
                   "next_action": "Verifica las comprobaciones indicadas o registra la decisión de integración manual"}
        if evaluation is None and prior and prior.get("binding") == binding:
            evaluation = prior | {"decision": "human_required", "reasons": reasons}
        fields = {} if evaluation is None else {"merge_evaluation": evaluation}
        self.worker.update("needs_input", **fields, reason=reasons[0], merge_intervention=request,
                           retry="pr_open", ci_retry_at=0, integration_approval=None)

    def wait(self, reason, *, bound=False):
        attempts = self.job.get("merge_waits", 0) + 1 if bound else 0
        if attempts > 3:
            self.human([reason])
            return
        delay = max(1, retry_delay(), self.worker.cfg["service"].get("ci_retry_seconds", 60))
        self.worker.update(STATE, reason=reason, merge_waits=attempts,
                           ci_retry_at=time.time() + delay)

    def current(self):
        job, worker = self.job, self.worker
        require_lease(worker.db, job)
        pr = ci.pull(job, self.lookup)
        if confirm(worker.db, job, pr) or not ci.validate(worker.db, job, pr, {ci.expected_head(job)}):
            return None
        settings = load(Path(worker.config))
        repo = registry_for(settings, job)
        issue = self.lookup(f"repos/{job['slug']}/issues/{job['number']}")
        if not repo or not eligible(issue, repo) or repo.get("base_branch", job["branch"]) != job["branch"]:
            self.human(["Confirma la rama base y la habilitación vigentes del repositorio"])
            return None
        if not ci.scope(worker.db, job, lambda endpoint: issue):
            return None
        binding = binding_for(job, pr, repo)
        remote_base, remote_head = references(job, self.lookup)
        if remote_head != binding["head"]:
            ci.head_conflict(worker.db, job, binding["head"], remote_head)
            return None
        if remote_base != binding["base"] or ci.merge_status(pr) == "pending":
            self.wait("Espera a que GitHub confirme las referencias y la mergeabilidad del PR", bound=True)
            return None
        if ci.merge_status(pr) == "conflicting":
            worker.update("pr_open", reason="La base presenta un conflicto", ci_retry_at=0,
                          integration_approval=None)
            return None
        return pr, binding, repo, settings

    def invalidate(self, binding):
        self.worker.update("pr_open", reason="Cambió el contexto de integración. Se requiere otra evaluación",
                           integration_approval=None, merge_intervention=None, merge_waits=0,
                           ci_retry_at=0, merge_invalidation={"binding": binding, "at": timestamp()})

    def run(self):
        try:
            self.process()
        except TaskStopped:
            raise
        except INTEGRATION_ERRORS as exc:
            reason = "Integración pendiente: " + ci.excerpt(str(exc), 500)
            if http_status(exc) in {401, 403, 404, 405, 422} and not retry_delay():
                self.human([reason, "Verifica los permisos, protecciones y condiciones del PR"])
            else:
                self.wait(reason, bound=True)

    def process(self):
        job, worker = self.job, self.worker
        current = self.current()
        if current is None:
            return
        pr, binding, repo, settings = current
        policy = policy_for(repo)
        if not policy["enabled"]:
            self.human(["La política del repositorio requiere integración manual"], binding=binding)
            return
        snapshot, failures = ci.observe(job, binding["head"], self.lookup)
        snapshot = filtered(snapshot)
        if failures or snapshot["state"] != "success":
            worker.update("pr_open", ci=snapshot, ci_retry_at=0, integration_approval=None,
                          merge_evaluation=None)
            return
        previous = job.get("merge_evaluation") or {}
        remote = ci_evidence(snapshot)
        rules = self.branch_checks()
        evidence_digest = digest([snapshot, rules])
        review_config = digest([repo["routing"][STATE], settings["providers"][repo["routing"][STATE]]])
        if (previous.get("binding") != binding or previous.get("ci_digest") != evidence_digest
                or previous.get("review_config") != review_config):
            self.evaluate(binding, repo, settings, policy, remote, rules, evidence_digest, review_config)
            return
        if previous["decision"] != "auto_merge":
            self.human(previous["reasons"], binding=binding)
            return
        if self.conditional_update is None:
            self.human(["GitHub permite condicionar el head. Hace falta una garantía atómica de la base evaluada."],
                       binding=binding)
            return
        self.integrate(pr, binding, policy, evidence_digest, review_config)

    def branch_checks(self):
        errors = {}
        fetched = github_ci.branch_requirements(
            self.lookup, f"repos/{self.job['slug']}", self.job["branch"], errors,
        )
        return github_ci.required_checks(fetched, {}) | {"errors": errors}

    def evaluate(self, binding, repo, settings, policy, remote, rules, evidence_digest, review_config):
        worker, job = self.worker, self.job
        base, head = binding["base"], binding["head"]
        if not worker.store.exists():
            worker.git_init()
        for sha in (base, head):
            worker.git("fetch", "--no-tags", worker.url, sha)
        merge_bases = worker.git("merge-base", "--all", base, head).splitlines()
        if len(merge_bases) != 1:
            self.human(["Determina una base común inequívoca para revisar el diff"], binding=binding)
            return
        args = (merge_bases[0], head, "--")
        paths = worker.git("diff", "--name-only", "--no-renames", "-z", *args).rstrip("\0").split("\0")
        paths = [path for path in paths if path]
        with tempfile.TemporaryDirectory(dir=worker.root) as folder:
            patch = Path(folder) / "pr.patch"
            worker.git("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
                       "--output=" + str(patch), *args)
            diff = patch.read_bytes()
        criteria = (job.get("plan") or {}).get("criteria") or []
        evidence = {"binding": binding, "policy": policy, "paths": paths, "ci": remote,
                    "github_branch_checks": rules,
                    "ci_digest": evidence_digest, "review_config": review_config, "criteria": criteria,
                    "diff_digest": hashlib.sha256(diff).hexdigest()}
        reports = {}
        run = None
        if not criteria:
            self.record(evidence, None, ["Registra criterios de aceptación vigentes para esta issue"])
            return
        try:
            patch_text = diff.decode("utf-8")
        except UnicodeDecodeError:
            self.record(evidence, None, ["Revisa manualmente el diff que contiene texto con codificación desconocida"])
            return
        for attempt in range(2):
            if attempt:
                current = self.current()
                if current is None:
                    return
                if current[1] != binding:
                    self.invalidate(current[1])
                    return
            report = worker.test(head, registry=repo)
            reports[f"local:{attempt + 1}"] = report
            if not report.get("passed") or report.get("sha") != head:
                self.record(evidence | {"local": reports}, None, ["Corrige o repite los checks locales del head"])
                return
            workspace = worker.checkout(head)
            try:
                run = worker.role(STATE, head, workspace, registry=repo, configuration=settings,
                                  criteria=criteria, policy=policy, base=base, head=head,
                                  diff=patch_text, paths=paths,
                                  checks=reports, ci=remote, github_branch_checks=rules)
                result = validate_result(run["result"], binding)
            except TaskStopped:
                raise
            except INTEGRATION_ERRORS as exc:
                self.record(evidence | {"local": reports}, None,
                            ["Repite la revisión independiente con un resultado válido: "
                             + ci.excerpt(str(exc), 500)])
                return
            finally:
                shutil.rmtree(workspace)
            if not result["additional_verification"]:
                break
        reasons = blockers(result, policy | {
            "critical_paths": [*policy["critical_paths"], *repo.get("protected_paths", [])],
        }, paths, remote, reports, incorporates_base=merge_bases == [base])
        if any("\ufffd" in path for path in paths):
            reasons.append("Comprueba manualmente las rutas Git con codificación no reconocida")
        if not rules["known"]:
            reasons.append("Verifica los checks exigidos por las reglas y la protección en GitHub")
        if result["additional_verification"]:
            reasons.append("La verificación adicional no resolvió la incertidumbre")
        self.record(evidence | {"local": reports}, run, reasons)

    def record(self, evidence, run, reasons):
        evaluation = {
            "version": 1, **evidence, "at": timestamp(),
            "decision": "human_required" if reasons else "auto_merge", "reasons": reasons,
            "result": run["result"] if run else None,
            "reviewer": {key: run.get(key) for key in
                         ("provider", "model", "execution_id", "stage_attempt", "evidence_file")} if run else None,
        }
        if reasons:
            self.human(reasons, binding=evidence["binding"], evaluation=evaluation)
        else:
            self.worker.update(STATE, merge_evaluation=evaluation, merge_intervention=None,
                               merge_waits=0, ci_retry_at=0, reason="Revisión independiente favorable")

    def integrate(self, pr, binding, policy, evidence_digest, review_config):
        job, worker = self.job, self.worker
        meta = self.lookup(f"repos/{job['slug']}")
        method = policy["method"]
        if meta.get(f"allow_{method}_merge") is not True:
            self.human(["Selecciona un método de merge admitido por el repositorio"], binding=binding)
            return
        operation = "ready" if pr.get("draft") is True else "merge"
        if operation == "merge":
            if pr.get("draft") is not False or pr.get("mergeable_state") != "clean":
                self.wait("GitHub debe confirmar checks, revisiones y protecciones del PR listo", bound=True)
                return
        if pr.get("draft") not in (True, False):
            self.human(["Confirma si el PR sigue en borrador"], binding=binding)
            return
        snapshot, _ = ci.observe(job, binding["head"], self.lookup)
        if digest([filtered(snapshot), self.branch_checks()]) != evidence_digest:
            self.invalidate(binding)
            return
        current = self.current()
        if current is None:
            return
        latest, current_binding, repo, settings = current
        provider = repo.get("routing", {}).get(STATE)
        if (current_binding != binding
                or digest([provider, settings["providers"].get(provider)]) != review_config):
            self.invalidate(current_binding)
            return
        if latest.get("draft") != pr.get("draft") or latest.get("mergeable_state") != pr.get("mergeable_state"):
            self.wait("Cambió la condición de integración del PR")
            return
        prior = job.get("merge_intent") or {}
        count = prior["attempts"] + 1 if prior.get("binding") == binding and prior["operation"] == operation else 1
        if count > 3:
            self.human(["Comprueba en GitHub el resultado de la operación de integración"], binding=binding)
            return
        intent = {"version": 1, "binding": binding, "operation": operation, "method": method,
                  "attempts": count, "at": timestamp()}
        worker.update(STATE, merge_intent=intent)
        require_lease(worker.db, job)
        self.conditional_update(intent)
        observed = ci.pull(job, self.lookup)
        if confirm(worker.db, job, observed) or (operation == "ready" and not job.get("merge_intent")):
            return
        self.wait("Confirma en GitHub el resultado de la operación de integración", bound=True)
