const base = "/dashboard/";
const params = new URLSearchParams(location.search);
const slug = params.get("repo") || "";
const state = params.get("state") || "";
const selected = ["intervention", "pulls"].includes(params.get("view")) ? params.get("view") : "";
const jobID = identity(params.get("job"));
const pullID = identity(params.get("pull"));
const detail = Boolean(jobID || pullID);
const main = document.querySelector("#main");
const notice = document.querySelector("#notice");
const freshness = document.querySelector("#freshness");
const refreshButton = document.querySelector("#refresh");

const groups = {
  queued: "En cola",
  active: "Activos",
  blocked: "Bloqueados",
  done: "Terminados",
};
const states = {
  queued: "En cola", planning: "Planificación", building: "Construcción", checking: "Comprobaciones",
  reviewing: "Revisión", publishing: "Publicación", retry_wait: "En espera de reintento",
  pr_open: "PR abierto", merge_review: "Revisión de integración", needs_input: "Requiere intervención", blocked: "Bloqueado",
  merged: "Fusionado", closed: "Cerrado", cancelled: "Cancelado",
};
const categories = {
  queued: "queued", planning: "active", building: "active", checking: "active", reviewing: "active",
  publishing: "active", retry_wait: "active", pr_open: "active", merge_review: "active",
  needs_input: "blocked", blocked: "blocked",
  merged: "done", closed: "done", cancelled: "done",
};
const roles = { plan: "Planificación", build: "Construcción", checks: "Comprobaciones", review: "Revisión",
  publish: "Publicación", merge_review: "Revisión de integración" };
const statuses = {
  active: "En ejecución", completed: "Terminado", failed: "Fallido", interrupted: "Interrumpido",
  cancelled: "Cancelado", pending: "Pendiente", needs_input: "Requiere intervención",
  changes_requested: "Cambios solicitados", in_progress: "En ejecución", queued: "En cola",
};
const eventNames = {
  ...states, started: "Intento iniciado", finished: "Intento finalizado",
  interrupted: "Intento interrumpido", ci_diagnostic: "Diagnóstico de CI",
  ci_observation: "Observación de CI", ci_started: "CI iniciado", ci_finished: "CI terminado",
  ci_state: "Cambio de CI", pr_head: "Cambio de head", pr_discovered: "PR observado",
  pr_state: "Cambio de estado del PR", observation_error: "Error de sondeo",
  observation_recovered: "Sondeo recuperado", required_checks: "Cambio de checks requeridos",
};
const decisionNames = {
  record_manual_merge: "Registrar decisión de integración manual", retry_merge_review: "Repetir revisión de integración",
  resume: "Reanudar", resume_planning: "Reanudar planificación", allow_paths: "Autorizar rutas",
  rounds: "Ampliar rondas", reuse_workspace: "Recuperar copia", head: "Aceptar head",
  resolved_commit: "Aceptar commit resuelto", link_pr: "Vincular PR",
  answer: "Responder pregunta", confirm_scope: "Confirmar alcance", resolve: "Confirmar causa resuelta",
  assign_provider: "Reasignar proveedor", add_rounds: "Ampliar rondas", extend_timeout: "Ampliar plazo y recuperar copia",
  add_recoveries: "Ampliar presupuesto de recuperación", accept_head: "Aceptar head",
};
const decisionResults = { accepted: "Aceptada", applied: "Aplicada", rejected: "Rechazada" };
const recoveryNames = {
  retry: "Reintentar etapa", resume_build: "Retomar construcción", resolve_base: "Resolver conflicto de base",
  refresh_base: "Integrar el avance de la base",
};
const availability = { unknown: "Desconocido", pending: "Pendiente", not_applicable: "No aplica" };
const dateFormatter = new Intl.DateTimeFormat("es", { dateStyle: "medium", timeStyle: "medium" });

function identity(value) {
  return /^\d{1,18}\/\d{1,18}$/.test(value || "") ? value : "";
}

function node(tag, content, attributes = {}) {
  const result = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, value);
  for (const child of Array.isArray(content) ? content.flat(Infinity) : [content]) {
    if (child !== null && child !== undefined) result.append(child);
  }
  return result;
}

function label(names, key) {
  return Object.hasOwn(names, key) ? names[key] : value(key);
}

function value(item, fallback = "Desconocido") {
  if (item === true) return "Sí";
  if (item === false) return "No";
  return item == null || item === "" ? fallback : String(item);
}

function dateValue(item) {
  if (typeof item === "number") return new Date(item * 1000);
  if (typeof item === "string" && /^\d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d$/.test(item)) {
    return new Date(item.replace(" ", "T") + "Z");
  }
  return new Date(item ?? NaN);
}

function when(item) {
  const date = dateValue(item);
  return Number.isNaN(date.getTime()) ? value(item) : dateFormatter.format(date);
}

function route(options = {}) {
  const query = new URLSearchParams();
  for (const [key, item] of Object.entries({ repo: slug, state, view: selected, ...options })) {
    if (item) query.set(key, item);
  }
  return base + (query.size ? "?" + query : "");
}

function anchor(text, href) {
  return node("a", text, { href });
}

function github(text, href) {
  if (!href) return null;
  try {
    const url = new URL(href);
    if (url.origin === "https://github.com" || url.origin === "https://api.github.com") {
      return anchor(text, url.origin + url.pathname);
    }
  } catch {
    return null;
  }
  return null;
}

function fields(values) {
  return node("dl", values.map(([key, item]) => node("div", [
    node("dt", key), node("dd", item instanceof Node ? item : value(item)),
  ])), { class: "fields" });
}

function sha(item) {
  return item ? node("code", item) : "Desconocido";
}

function badge(text, category) {
  return node("span", text, { class: "badge " + (category || "") });
}

function textList(items) {
  return node("ul", items.map(item => node("li", item)));
}

function disclosure(title, children, key) {
  return node("details", [node("summary", title), children], key ? { id: key } : {});
}

function section(title, children, className = "section") {
  return node("section", [node("h2", title), children], { class: className });
}

function nonempty(items, fallback) {
  return items.length ? items : node("p", fallback, { class: "meta" });
}

async function request(path, signal) {
  const response = await fetch(path, { signal, cache: "no-store" });
  if (!response.ok) {
    const error = new Error("HTTP " + response.status);
    error.status = response.status;
    throw error;
  }
  const payload = await response.json();
  if (payload.version !== 1 || !Object.hasOwn(payload, "data")) throw new Error("Respuesta de observación inválida");
  return payload;
}

let credential = "";
let actionContext = null;
let actionNotice = "";
let actionError = false;
let sending = false;
let submission = null;
let receipt = null;
let draft = { kind: "", evidence: "", confirmed: false, options: {} };
const actionURL = "/v1/jobs/" + jobID + "/actions";

async function actionRequest(signal, attempt, receiptID) {
  const headers = { Authorization: "Bearer " + credential };
  const options = { signal, headers, cache: "no-store" };
  if (attempt) {
    headers["Idempotency-Key"] = attempt.key;
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(attempt.payload);
  }
  const response = await fetch(receiptID ? actionURL + "/" + receiptID : actionURL, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error?.message || "Error HTTP " + response.status);
    error.status = response.status;
    error.code = payload.error?.code;
    throw error;
  }
  if (payload.version !== 1 || !payload.data) throw new Error("Respuesta de acciones inválida");
  return payload.data;
}

async function readActions(signal, job) {
  if (!credential || !job.actions_enabled || !job.intervention.pending) return null;
  try {
    return await actionRequest(signal);
  } catch (error) {
    if (error.status === 401) credential = "";
    actionNotice = error.status ? error.message : "No se pudieron leer las acciones.";
    actionError = true;
    return null;
  }
}

async function readReceipt(signal, job) {
  if (!credential || !job.actions_enabled || receipt?.status !== "accepted") return;
  const id = receipt.id;
  try {
    const current = await actionRequest(signal, null, id);
    if (receipt?.id === id) receipt = current;
  } catch {
    // Keep displaying the confirmed receipt while the service is temporarily unavailable.
  }
}

async function collection(path, signal, filters = {}) {
  const query = new URLSearchParams(filters);
  query.set("limit", 100);
  const entries = [];
  let offset = 0;
  do {
    query.set("offset", offset);
    const page = await request(path + "?" + query, signal);
    entries.push(...page.data);
    const next = page.pagination.next_offset;
    if (next !== null && (!Number.isSafeInteger(next) || next <= offset)) throw new Error("Paginación inválida");
    offset = next;
  } while (offset !== null);
  return entries;
}

function currentFilters() {
  return { ...(slug && { slug }), ...(Object.hasOwn(states, state) && { state }) };
}

async function readPull(id, signal, required) {
  try {
    const path = "/v1/pulls/" + id;
    const { data: pull } = await request(path, signal);
    const [snapshots, events] = await Promise.all([
      collection(path + "/ci", signal), collection(path + "/events", signal),
    ]);
    return { pull, snapshots, events };
  } catch (error) {
    if (error.status === 404 && !required) return null;
    throw error;
  }
}

async function read(signal) {
  if (jobID) {
    const path = "/v1/jobs/" + jobID;
    const { data: job } = await request(path, signal);
    const [attempts, events, ci, actions] = await Promise.all([
      collection(path + "/attempts", signal), collection(path + "/events", signal),
      job.pr ? readPull(job.repo_id + "/" + job.pr.number, signal, false) : null,
      readActions(signal, job),
      readReceipt(signal, job),
    ]);
    return { job, attempts, events, ci, actions };
  }
  if (pullID) {
    const record = await readPull(pullID, signal, true);
    if (!record.pull.job?.number) return record;
    const path = "/v1/jobs/" + record.pull.repo_id + "/" + record.pull.job.number;
    try {
      const { data: job } = await request(path, signal);
      return { ...record, job };
    } catch (error) {
      if (error.status === 404) return record;
      throw error;
    }
  }
  const registry = collection("/v1/repositories", signal);
  if (selected === "pulls") {
    const repo = slug ? { slug } : {};
    const [repositories, pulls, jobs] = await Promise.all([
      registry, collection("/v1/pulls", signal, { ...repo, state: "open" }), collection("/v1/jobs", signal, repo),
    ]);
    return { repositories, pulls, jobs };
  }
  const filters = currentFilters();
  if (selected === "intervention") {
    const [repositories, questions, blocked] = await Promise.all([
      registry, collection("/v1/jobs", signal, { ...filters, state: "needs_input" }),
      collection("/v1/jobs", signal, { ...filters, state: "blocked" }),
    ]);
    return { repositories, jobs: [...questions, ...blocked] };
  }
  const [repositories, jobs] = await Promise.all([registry, collection("/v1/jobs", signal, filters)]);
  return { repositories, jobs };
}

function nav() {
  const links = [["", "Trabajos"], ["intervention", "Intervenciones"], ["pulls", "PR y CI"]];
  document.querySelector("#navigation").append(...links.map(([view, title]) => {
    const link = anchor(title, route({ view, state: view ? "" : state }));
    if (view === selected) link.setAttribute("aria-current", "page");
    return link;
  }));
}

function select(name, title, choices, current) {
  const input = node("select", choices.map(([key, text]) => {
    const option = node("option", text, { value: key });
    option.selected = key === current;
    return option;
  }), { name });
  return node("label", [node("span", title), input]);
}

function filterForm(repositories, data) {
  const slugs = [...new Set([slug, ...repositories.map(repo => repo.slug), ...data.map(repo => repo.slug)])]
    .filter(Boolean).sort();
  return node("form", [
    selected ? node("input", null, { type: "hidden", name: "view", value: selected }) : null,
    select("repo", "Repositorio", [["", "Todos"], ...slugs.map(repo => [repo, repo])], slug),
    selected ? null : select("state", "Estado", [["", "Todos"], ...Object.entries(states)], state),
    node("button", "Filtrar", { type: "submit" }),
  ], { class: "filters", method: "get", action: base });
}

function interventionType(job) {
  if (job.intervention.scope_changed) return "Cambio de alcance";
  if (job.intervention.questions.length) return "Pregunta";
  return "Bloqueo";
}

function jobRow(job) {
  const id = job.repo_id + "/" + job.number;
  return node("li", [
    node("div", [
      anchor("#" + job.number + " · " + value(job.title, "Sin título registrado"), route({ job: id })),
      job.reason && job.intervention.pending ? node("p", job.reason, { class: "meta" }) : null,
      node("div", [github("Issue en GitHub", job.issue?.url), github("PR #" + job.pr?.number, job.pr?.url)], { class: "links meta" }),
    ]),
    badge(selected === "intervention" ? interventionType(job) : label(states, job.state), categories[job.state]),
  ]);
}

function jobsPage({ repositories, jobs }) {
  const intervention = selected === "intervention";
  const counts = Object.entries(groups).map(([key, title]) => node("div", [
    node("strong", String(jobs.filter(job => categories[job.state] === key).length)), title,
  ], { class: "stat" }));
  const byRepo = new Map();
  for (const job of jobs) {
    if (!byRepo.has(job.slug)) byRepo.set(job.slug, []);
    byRepo.get(job.slug).push(job);
  }
  return [
    node("h1", intervention ? "Intervenciones pendientes" : "Trabajos de la fábrica"),
    filterForm(repositories, jobs),
    intervention ? null : node("div", counts, { class: "summary-grid" }),
    nonempty([...byRepo.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([repo, entries]) => {
      const remaining = entries.filter(job => !Object.hasOwn(groups, categories[job.state]));
      const lists = Object.entries(groups).map(([key, title]) => {
        const items = entries.filter(job => categories[job.state] === key);
        return items.length ? node("div", [node("h3", title), node("ul", items.map(jobRow), { class: "rows" })]) : null;
      });
      return section(repo, intervention ? node("ul", entries.map(jobRow), { class: "rows" }) : [
        lists, remaining.length ? node("ul", remaining.map(jobRow), { class: "rows" }) : null,
      ], "repo");
    }), intervention ? "No hay intervenciones pendientes para esta selección." : "No hay trabajos para estos filtros."),
  ];
}

const passing = new Set(["success", "neutral", "skipped"]);
const failures = new Set(["failure", "error", "timed_out", "cancelled", "action_required", "startup_failure", "stale"]);
const ciStates = {
  pending: ["CI pendiente", ""], running: ["CI en ejecución", "active"], failed: ["CI fallido", "failed"],
  success: ["CI correcto", "done"], blocked: ["CI pendiente de intervención", "blocked"],
};

function ciStatus(snapshot, job, head) {
  if (job?.intervention?.pending && (job.intervention.ci || job.repair)) return ciStates.blocked;
  const current = [
    ...Object.values(snapshot?.checks || {}).filter(check => check.current !== false),
    ...Object.values(snapshot?.runs || {}).filter(run => run.current !== false).map(run => run.run || run),
  ];
  const local = job?.ci?.sha === head ? job?.ci : null;
  const observed = current.length ? current : local?.checks || [];
  if (observed.some(item => (item.phase || item.status) === "in_progress")) return ciStates.running;
  if (observed.some(item => failures.has(item.conclusion)) || (!current.length && local?.state === "failed")) return ciStates.failed;
  if (local?.state === "success" && !current.length) return ciStates.success;
  if (current.length && snapshot?.required?.known && !snapshot.required.missing?.length && !snapshot.required.unverified?.length
      && current.every(item => passing.has(item.conclusion))) return ciStates.success;
  if (local?.state === "success" && current.every(item => passing.has(item.conclusion))) return ciStates.success;
  return ciStates.pending;
}

function pullList({ repositories, pulls, jobs }) {
  const linked = new Map(jobs.filter(job => job.pr).map(job => [job.repo_id + "/" + job.pr.number, job]));
  return [
    node("h1", "PR abiertos y CI"),
    filterForm(repositories, pulls),
    nonempty(pulls.length ? [node("ul", pulls.map(pull => {
      const id = pull.repo_id + "/" + pull.number;
      const job = linked.get(id);
      return node("li", [
        node("div", [
          anchor(pull.slug + " #" + pull.number, route({ pull: id })),
          node("div", [github("PR en GitHub", pull.url), job ? anchor("Trabajo #" + job.number,
            route({ job: job.repo_id + "/" + job.number })) : null], { class: "links meta" }),
          node("p", ["Head ", sha(pull.head?.sha)], { class: "meta" }),
          Object.keys(pull.ci?.errors || {}).length || Object.keys(pull.source_observation?.errors || {}).length
            ? node("p", "Sondeo con errores", { class: "meta" }) : null,
        ]),
        badge(...ciStatus(pull.ci, job, pull.head?.sha)),
      ]);
    }), { class: "rows panel" })] : [], "No hay PR abiertos para esta selección."),
  ];
}

function errorList(errors) {
  const entries = Object.entries(errors || {});
  return entries.length ? textList(entries.map(([source, error]) =>
    source + " · " + value(error.message) + (error.status == null ? "" : " · HTTP " + error.status))) : null;
}

function diagnostic(record) {
  if (!record) return null;
  const kinds = {
    functional: "Funcional", infrastructure: "Infraestructura", configuration: "Configuración",
    access: "Acceso", quota: "Cuota", unknown: "Desconocida",
  };
  return node("div", [
    fields([["Causa", label(kinds, record.kind)], ["Evidencia", record.evidence],
      ["Resultado", record.output?.title], ["Check", record.check?.name]]),
    record.output?.summary ? node("p", record.output.summary) : null,
    record.output?.text ? node("pre", record.output.text) : null,
    record.log_excerpt ? disclosure("Extracto filtrado del log", node("pre", record.log_excerpt)) : null,
    record.annotations?.length ? textList(record.annotations.map(item =>
      [value(item.path), value(item.start_line), value(item.message)].join(" · "))) : null,
    record.errors?.length ? errorList(Object.fromEntries(record.errors.map(item =>
      [item.source, { message: item.error }]))) : null,
    node("div", [
      github("Detalles en GitHub", record.details_url), github("Anotaciones", record.annotations_url),
      github("Log de GitHub", record.log_url),
      (record.sources || []).map((url, index) => github("Fuente " + (index + 1), url)),
    ], { class: "links meta" }),
  ]);
}

function checkRow(check, head, observed) {
  const links = new Set([check.html_url, check.details_url, check.run_url, check.job_url, check.target_url,
    check.url, check.attempt_url, check.log_url, check.check_run_url, check.workflow_url].filter(Boolean));
  return node("li", [
    node("div", [
      node("strong", value(check.name)),
      fields([["Estado", label(statuses, check.status || check.phase)], ["Conclusión", check.conclusion],
        ["SHA", sha(head)], ["Intento", check.attempt ?? check.run_attempt],
        ["Última observación", when(observed)], ["Vigencia", check.current === false ? "Histórico" : "Vigente"]]),
      node("div", [...links].map((url, index) => github(index ? "Otra evidencia en GitHub" : "Ver en GitHub", url)),
        { class: "links meta" }),
      check.diagnostic ? disclosure("Diagnóstico del check", diagnostic(check.diagnostic)) : null,
    ]),
  ]);
}

function snapshotCard(snapshot, head) {
  const checks = Object.values(snapshot.checks || {});
  const runs = Object.values(snapshot.runs || {}).map(run => ({
    ...(run.run || {}), current: run.current, phase: run.phase, attempt: run.attempt,
    attempt_url: run.attempt_url, log_url: run.log_url, workflow_url: run.workflow_url,
  }));
  const missing = snapshot.required?.missing || [];
  const unverified = snapshot.required?.unverified || [];
  return node("article", [
    node("h3", snapshot.sha === head ? "Head actual" : "Head histórico"),
    fields([["SHA", sha(snapshot.sha)], ["Última observación", when(snapshot.observed_at)],
      ["Última lectura de checks", when(snapshot.last_success_at?.check_runs)],
      ["Última lectura de workflows", when(snapshot.last_success_at?.workflow_runs)],
      ["Checks requeridos verificados", snapshot.required?.known]]),
    missing.length ? node("p", "Checks requeridos pendientes · " + missing.map(item => item.name).join(", ")) : null,
    unverified.length ? node("p", "Origen pendiente de verificar · " + unverified.map(item => item.name).join(", ")) : null,
    errorList(snapshot.errors),
    node("h3", "Checks"),
    node("ul", checks.map(check => checkRow(check, snapshot.sha, snapshot.observed_at)), { class: "rows" }),
    checks.length ? null : node("p", "Sin checks observados.", { class: "meta" }),
    node("h3", "Workflows"),
    node("ul", runs.map(run => checkRow(run, snapshot.sha, snapshot.observed_at)), { class: "rows" }),
    runs.length ? null : node("p", "Sin workflows observados.", { class: "meta" }),
  ], { class: "snapshot" });
}

function ciHistory({ pull, snapshots }, job) {
  const head = pull.head?.sha;
  const ordered = [...snapshots].sort((left, right) =>
    Number(right.sha === head) - Number(left.sha === head) || String(right.observed_at).localeCompare(String(left.observed_at)));
  return [
    badge(...ciStatus(pull.ci, job, head)),
    fields([["Head observado", sha(head)], ["Último sondeo del PR", when(pull.observed_at)],
      ["Cambio de errores del repositorio", when(pull.source_observation?.changed_at)]]),
    errorList(pull.errors), errorList(pull.source_observation?.errors),
    nonempty(ordered.map(record => snapshotCard(record, head)), "Aún no se observaron checks ni workflows para este PR."),
  ];
}

function reference(job, ref, index) {
  const target = "/v1/jobs/" + job.repo_id + "/" + job.number;
  const id = String(ref.artifact_id || ref.attempt_id || "");
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(id)) return null;
  return node("li", [
    anchor((ref.artifact_id ? "Artefacto filtrado " : "Resultado del intento ") + (index + 1),
      target + (ref.artifact_id ? "/artifacts/" : "/attempts/") + id),
    " · SHA ", sha(ref.sha),
  ]);
}

function explanation(record, job) {
  const report = record.explanation || {};
  return [
    node("h3", "Resumen del razonamiento"),
    node("p", value(report.summary, label(availability, report.availability))),
    fields([["Motivo de reintento o bloqueo", report.reason || record.error], ["Procedencia del resumen", report.source]]),
    report.decisions?.length ? [node("h3", "Decisiones"), textList(report.decisions)] : null,
    report.criteria?.length ? [node("h3", "Criterios considerados"), textList(report.criteria)] : null,
    report.evidence?.length ? [node("h3", "Evidencia consultada"),
      node("ul", report.evidence.map((ref, index) => reference(job, ref, index)))] : null,
  ];
}

function identityFields(stage) {
  const model = stage.model || {};
  return [
    ["Rol", label(roles, stage.role)],
    ["Agente o ejecutor", value(stage.executor, label(availability, stage.identity_availability))],
    ["Proveedor", value(stage.provider, model.availability === "not_applicable" ? "No aplica" : "Desconocido")],
    ["ID de ejecución", value(stage.execution_id, label(availability, stage.execution_id_availability))],
    ["Fuente del ID", stage.execution_id_source],
    ["Versión de CLI", value(stage.cli_version, label(availability, stage.cli_version_availability))],
    ["Modelo solicitado", value(model.requested, model.availability === "not_applicable" ? "No aplica" : "Desconocido")],
    ["Modelo efectivo", value(model.effective, model.availability === "unknown" ? "No comunicado" : label(availability, model.availability))],
    ["Procedencia del modelo efectivo", value(model.source, label(availability, model.availability))],
    ...(stage.legacy_model ? [["Modelo en el registro histórico", stage.legacy_model]] : []),
  ];
}

function checkReport(report) {
  if (!report) return null;
  return [
    node("h3", "Resultado de checks locales"),
    fields([["SHA", sha(report.sha)], ["Correcto", report.passed], ["Tipo de fallo", report.failure_kind],
      ["Perfil", report.profile_sha256], ["Servicio", report.service]]),
    report.commands?.length ? node("ol", report.commands.map(command =>
      node("li", "Código " + value(command.code) + " · Duración " + value(command.seconds) + " s"))) : null,
  ];
}

function attemptChanges(stage, previous) {
  if (!previous) return null;
  const pairs = [
    ["Agente", previous.executor, stage.executor], ["Proveedor", previous.provider, stage.provider],
    ["Modelo solicitado", previous.model?.requested, stage.model?.requested],
    ["Modelo efectivo", previous.model?.effective, stage.model?.effective],
    ["Decisión", previous.explanation?.decisions?.join(" · ") || previous.explanation?.summary,
      stage.explanation?.decisions?.join(" · ") || stage.explanation?.summary],
  ];
  const changed = pairs.filter(([, before, after]) => before !== after);
  return [
    node("p", ["Intento anterior ", anchor(previous.id, "#attempt-" + previous.id)], { class: "meta" }),
    changed.length ? textList(changed.map(([name, before, after]) => name + " · " + value(before) + " → " + value(after))) : null,
  ];
}

function attemptCard(stage, job, previous) {
  const measurements = stage.usage?.measurements || [];
  return node("article", [
    node("div", [
      node("h3", label(roles, stage.role) + " · " + value(stage.executor)),
      badge(label(statuses, stage.status), stage.status === "active" ? "active" : stage.status === "failed" ? "failed" : ""),
    ], { class: "title-row" }),
    fields([...identityFields(stage), ["Intento", stage.id], ["Inicio", when(stage.started_at)],
      ["Fin", when(stage.finished_at)], ["SHA de entrada", sha(stage.input_sha)],
      ["SHA candidato", sha(stage.candidate_sha)], ["Plazo asignado (s)", stage.configured_timeout_seconds],
      ...(stage.error_type === "CommandTimeout" ? [["Plazo agotado (s)", stage.timeout_seconds]] : []),
      ["CI de origen", sha(stage.ci_origin?.sha)], ["CI resultante", sha(stage.ci_result?.sha)]]),
    attemptChanges(stage, previous),
    ["checks", "publish"].includes(stage.role) ? fields([["Comandos registrados",
      (stage.operations?.length ? stage.operations : stage.executables)?.join(", ")]]) : null,
    stage.role === "checks" ? checkReport(stage.result) : null,
    stage.result?.question ? fields([["Pregunta", stage.result.question]]) : null,
    stage.result?.verdict ? fields([["Veredicto", stage.result.verdict]]) : null,
    explanation(stage, job),
    node("h3", "Uso comunicado"),
    measurements.length ? textList(measurements.map(item =>
      [value(item.name), value(item.value) + " " + value(item.unit), item.kind, item.source].filter(Boolean).join(" · ")))
      : node("p", label(availability, stage.usage?.availability)),
  ], { class: "attempt", id: "attempt-" + stage.id });
}

function interventionPanel(job, failed) {
  const entry = job.intervention;
  if (!entry.pending) return null;
  const checkpoint = entry.build_checkpoint;
  const conflict = job.base_conflict;
  const context = actionContext?.revision === job.revision ? actionContext : null;
  const compatible = context?.compatible_build_copies;
  const timeout = context?.build_timeout || (failed?.error_type === "CommandTimeout"
    ? { stage: failed.role, seconds: failed.timeout_seconds } : null);
  return section("Intervención · " + interventionType(job), [
    fields([["Causa", entry.reason], ["Candidato", sha(job.candidate_sha)],
      ["Etapa registrada para la reanudación", label(roles, entry.retry_stage)],
      ...(entry.head_conflict ? [["Head esperado", sha(entry.head_conflict.expected)],
        ["Head observado", sha(entry.head_conflict.observed)], ["Referencia remota", sha(entry.head_conflict.remote)]] : [])]),
    conflict ? [node("h3", "Conflicto con la rama base"),
      fields([["Base vigente observada", sha(job.observed_base || conflict.base)],
        ["Base del conflicto registrado", job.observed_base && sha(conflict.base)],
        ["Candidato conservado", sha(conflict.candidate)]]),
      textList(conflict.paths)] : null,
    timeout ? fields([
      ["Etapa interrumpida", label(roles, timeout.stage)], ["Plazo agotado", timeout.seconds == null
        ? null : timeout.seconds + " s"], ["Rondas consumidas", job.limits?.rounds],
      ["Rondas autorizadas", job.limits?.build_limit],
    ]) : null,
    (entry.questions || []).map(question => node("blockquote", [
      node("p", question.text),
      question.attempt_id ? anchor("Intento que formuló la pregunta", "#attempt-" + question.attempt_id) : null,
    ])),
    entry.protected_paths?.length ? [node("h3", "Archivos protegidos registrados"), textList(entry.protected_paths)] : null,
    checkpoint ? [
      node("h3", "Copia de construcción registrada"),
      fields([["SHA de entrada", sha(checkpoint.input_sha)], ["Ronda de la copia", checkpoint.round],
        ["Coincide con el SHA actual", checkpoint.matches_input], ["Coincide con el alcance actual", checkpoint.matches_scope],
        ["Intento interrumpido", checkpoint.failed_attempt]]),
    ] : null,
    timeout ? fields([
      ["Disponibilidad de copia compatible", compatible == null ? "Consulta las acciones del operador"
        : compatible > 0 ? "Disponible (" + compatible + ")" : "Sin copias compatibles"],
    ]) : null,
    node("div", [
      github("Comparar base y candidato", job.published_sha && job.published_sha === job.candidate_sha
        && "https://github.com/" + job.slug + "/compare/" + job.base_sha + "..." + job.candidate_sha),
      github("Revisar archivos del PR", job.pr?.url && job.pr.url + "/files"),
      github("Revisar alcance en GitHub", job.issue?.url),
      anchor("Consultar cronología y evidencia", "#timeline"),
    ], { class: "links" }),
    actionPanel(job),
  ], "panel");
}

function authForm() {
  const token = node("input", null, { type: "password", required: "", autocomplete: "off", name: "token" });
  const form = node("form", [node("label", ["Token del operador", token]),
    node("button", "Consultar acciones", { type: "submit" })], { class: "decision-form" });
  form.addEventListener("submit", event => {
    event.preventDefault();
    credential = token.value.trim();
    token.value = "";
    actionNotice = "Consultando acciones…";
    actionError = false;
    paint();
    refreshAfterPending();
  });
  return form;
}

function optionControl(name, title, choices, initial) {
  const select = node("select", choices.map(([key, text]) => node("option", text, { value: key })), { name });
  select.value = choices.some(([key]) => key === draft.options[name]) ? draft.options[name] : String(initial);
  select.addEventListener("change", () => { draft.options[name] = select.value; });
  return node("label", [title, select]);
}

function numberControl(name, title, minimum, maximum, effect, currentLimit) {
  const input = node("input", null, { name, type: "number", min: minimum, max: maximum, required: "" });
  input.value = draft.options[name] ?? minimum;
  input.addEventListener("input", () => {
    draft.options[name] = input.value;
    if (effect) effect.textContent = input.validity.valid ? "Nuevo límite · " + (currentLimit + Number(input.value)) : "";
  });
  return node("label", [title, input]);
}

function decisionForm(job, offers) {
  const offer = offers.find(item => item.kind === draft.kind) || offers[0];
  const action = node("select", offers.map(item => node("option", label(decisionNames, item.kind),
    { value: item.kind })), { name: "kind" });
  action.value = offer.kind;
  action.addEventListener("change", () => {
    draft = { ...draft, kind: action.value, confirmed: false, options: {} };
    paint();
  });
  const evidence = node("textarea", null, { name: "evidence", required: "", maxlength: 4000, rows: 4 });
  evidence.value = draft.evidence;
  evidence.addEventListener("input", () => { draft.evidence = evidence.value; });
  const confirm = node("input", null, { type: "checkbox", name: "confirm", required: "" });
  confirm.checked = draft.confirmed;
  confirm.addEventListener("change", () => { draft.confirmed = confirm.checked; });
  const controls = [];
  if (offer.kind === "confirm_scope") {
    controls.push(fields([["Título anterior", offer.scope?.previous?.title],
      ["Título observado", offer.scope?.observed?.title]]),
    disclosure("Extractos filtrados del alcance", [
      node("h3", "Anterior"), node("pre", value(offer.scope?.previous?.body)),
      node("h3", "Observado"), node("pre", value(offer.scope?.observed?.body)),
    ]));
  }
  if (offer.kind === "allow_paths") {
    const changes = new Map((offer.changes || []).map(item => [item.path, item]));
    controls.push(node("p", "Archivos exactos que se autorizan para este trabajo"),
      textList(offer.paths.map(path => {
        const record = changes.get(path);
        const lines = record && record.added != null && record.removed != null
          ? " · +" + record.added + " / −" + record.removed + " líneas" : "";
        return [node("code", path), lines];
      })));
  }
  if (offer.kind === "assign_provider") {
    const role = Object.hasOwn(offer.roles, draft.options.role) ? draft.options.role : offer.role;
    const roleControl = optionControl("role", "Rol", Object.keys(offer.roles).map(key => [key, label(roles, key)]), role);
    roleControl.querySelector("select").addEventListener("change", () => {
      delete draft.options.provider;
      paint();
    });
    controls.push(roleControl, fields([["Proveedor actual", offer.routing[role]]]),
      optionControl("provider", "Nuevo proveedor", offer.roles[role].map(name => [name, name]), offer.roles[role][0]));
  }
  if (["add_rounds", "add_recoveries"].includes(offer.kind)) {
    const effect = node("p", "Nuevo límite · " + (offer.current_limit + Number(draft.options.extra ?? 1)));
    controls.push(fields([["Límite actual", offer.current_limit]]),
      numberControl("extra", offer.kind === "add_rounds" ? "Rondas adicionales" : "Recuperaciones adicionales",
        1, offer.maximum_extra, effect, offer.current_limit), effect);
  }
  if (offer.kind === "resolved_commit") {
    controls.push(optionControl("sha", "Commit de resolución conservado",
      offer.commits.map(commit => [commit, commit]), offer.commits[0]));
  }
  if (offer.kind === "accept_head") controls.push(fields([["Head observado", sha(offer.head)]]));
  if (offer.kind === "record_manual_merge") {
    controls.push(fields([["PR", offer.binding.pr], ["Base", sha(offer.binding.base)],
      ["Head", sha(offer.binding.head)], ["Alcance", offer.binding.scope], ["Política", offer.binding.policy]]));
  }
  if (offer.kind === "extend_timeout") {
    controls.push(fields([["Plazo anterior", offer.previous_seconds + " s"]]),
      numberControl("seconds", "Nuevo plazo de construcción (segundos)", offer.minimum_seconds, offer.maximum_seconds));
  }
  if (["extend_timeout", "reuse_workspace"].includes(offer.kind)) {
    controls.push(optionControl("reference", "Copia de construcción compatible", offer.copies.map(copy => [copy.reference,
        (copy.current ? "Actual" : "Histórica") + " · Ronda " + copy.round + " · " + copy.input_sha +
        " · Intento " + copy.failed_attempt]), offer.copies[0].reference));
  }
  const coherent = actionContext.revision === job.revision;
  const form = node("form", [node("fieldset", [
    node("legend", "Decisión de " + actionContext.operator), node("label", ["Acción", action]),
    fields([["Estado previsto al continuar", label(states, offer.resume_state)]]), controls,
    node("label", [offer.kind === "answer" ? "Respuesta" : "Motivo o evidencia", evidence]),
    node("label", [confirm, offer.kind === "allow_paths"
      ? "Confirmo la autorización de todos los archivos enumerados"
      : "He revisado el contexto y confirmo la decisión"], { class: "confirm" }),
    node("button", "Enviar decisión", { type: "submit" }),
  ], sending || submission || !coherent ? { disabled: "" } : {})], { class: "decision-form" });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!coherent || sending || submission || !confirm.checked) return;
    const data = new FormData(form);
    const options = {};
    if (offer.kind === "allow_paths") options.paths = offer.paths;
    if (offer.kind === "assign_provider") {
      options.role = data.get("role");
      options.provider = data.get("provider");
    }
    if (["add_rounds", "add_recoveries"].includes(offer.kind)) options.extra = Number(data.get("extra"));
    if (offer.kind === "resolved_commit") options.sha = data.get("sha");
    if (offer.kind === "accept_head") options.sha = offer.head;
    if (offer.kind === "extend_timeout") options.seconds = Number(data.get("seconds"));
    if (["extend_timeout", "reuse_workspace"].includes(offer.kind)) {
      options.reference = data.get("reference");
    }
    submission = { key: crypto.randomUUID(), payload: {
      kind: offer.kind, revision: job.revision, evidence: evidence.value, confirm: confirm.checked, options,
    } };
    sendDecision();
  });
  return form;
}

function actionPanel(job) {
  if (!job.actions_enabled) return node("p", "Acciones deshabilitadas en este servicio.", { class: "meta" });
  const offers = actionContext?.actions || [];
  const retry = node("button", "Reintentar el mismo envío", { type: "button", name: "retry" });
  retry.addEventListener("click", sendDecision);
  return node("div", [
    node("p", actionNotice, { role: "status", class: actionError ? "action-status error" : "action-status" }),
    actionContext && actionContext.revision !== job.revision && job.intervention.pending
      ? node("p", "El contexto acaba de cambiar. Actualiza el trabajo y revisa la decisión.", { class: "meta" }) : null,
    !credential ? authForm() : null,
    credential && (actionContext?.pending || receipt?.status === "accepted")
      ? node("p", "El controlador tiene una decisión pendiente de aplicación.")
      : credential && actionContext && !offers.length ? node("p", "El servidor no ofrece acciones para este estado.") : null,
    credential && offers.length && receipt?.status !== "accepted" ? decisionForm(job, offers) : null,
    credential && submission && !sending ? retry : null,
  ], { id: "operator-actions" });
}

async function sendDecision() {
  if (!submission || sending || !credential) return;
  sending = true;
  actionNotice = "Enviando decisión…";
  actionError = false;
  paint();
  try {
    receipt = await actionRequest(AbortSignal.timeout(15000), submission);
    draft = { kind: "", evidence: "", confirmed: false, options: {} };
    submission = null;
    actionContext = null;
    actionNotice = "";
  } catch (error) {
    actionError = true;
    const conflict = ["revision_conflict", "idempotency_conflict", "incompatible_state"].includes(error.code);
    if (conflict || error.status === 422) {
      submission = null;
      draft.confirmed = false;
    }
    if (conflict) {
      draft.options = {};
      actionContext = null;
    }
    if (error.status === 401) credential = "";
    actionNotice = error.status ? error.message : "No se confirmó el envío. Reintenta la misma decisión.";
  } finally {
    sending = false;
    paint();
    refreshAfterPending();
  }
}

function operationCard(operation, resultingState) {
  const decision = operation.decision || {};
  return node("div", [
    node("h3", label(decisionNames, decision.kind)),
    fields([["Operador", operation.executor || decision.operator], ["Fecha", when(operation.at || decision.at)],
      ["Origen", decision.origin], ["Revisión esperada", decision.expected_revision],
      ["Modelo", "No aplica"], ["SHA", sha(operation.sha)], ["Estado resultante", label(states, resultingState)],
      ["Motivo del bloqueo", decision.reason], ["Feedback", decision.evidence || operation.explanation?.reason],
      ["Recibo", decision.id],
      ["Resultado", label(decisionResults, decision.result || decision.outcome || label(decisionNames, decision.kind))],
      ["Código de error", decision.error_code],
      ["Estado reanudado", decision.resume_state ? label(states, decision.resume_state) : null]]),
    decision.rounds == null ? null : fields([["Rondas adicionales", decision.rounds], ["Nuevo límite", decision.round_limit]]),
    decision.provider ? fields([["Rol reasignado", label(roles, decision.role)],
      ["Proveedor anterior", decision.previous_provider], ["Proveedor elegido", decision.provider]]) : null,
    decision.timeout_seconds ? fields([["Plazo anterior", decision.previous_timeout_seconds + " s"],
      ["Plazo autorizado", decision.timeout_seconds + " s"], ["Referencia de copia", decision.workspace]]) : null,
    decision.paths?.length ? textList(decision.paths) : null,
    decision.question ? fields([["Pregunta", decision.question], ["Intento", decision.question_attempt],
      ["Respuesta", decision.answer]]) : null,
    decision.recovery_limit == null ? null : fields([["Recuperaciones añadidas", decision.recoveries],
      ["Nuevo límite", decision.recovery_limit]]),
    decision.previous_candidate ? fields([["Candidato anterior", sha(decision.previous_candidate)],
      ["Base anterior", sha(decision.previous_base)], ["Base verificada", sha(decision.base)]]) : null,
  ]);
}

function feedbackRecord(feedback) {
  if (!feedback) return null;
  if (typeof feedback === "string") return node("p", feedback);
  return [
    fields([["Origen del feedback", feedback.source], ["SHA", sha(feedback.sha)],
      ["Feedback", feedback.instruction || feedback.summary || feedback.reason || feedback.question || feedback.verdict]]),
    feedback.findings?.length ? textList(feedback.findings) : null,
    feedback.paths?.length ? textList(feedback.paths) : null,
    checkReport(feedback.checks),
    feedback.diagnostics?.map(diagnostic),
  ];
}

function jobEvent(event, attempts) {
  const record = event.detail || {};
  const attemptId = event.stage?.id || record.stage_attempt;
  const stage = event.stage || attempts.get(attemptId);
  const operations = event.operations || [];
  const operation = operations[0];
  const heading = label(eventNames, record.kind || record.stage_event || event.state);
  const identity = stage ? [label(roles, stage.role), stage.executor, stage.provider !== stage.executor ? stage.provider : null]
    : operation ? [label(decisionNames, operation.decision?.kind), operation.executor] : [];
  return {
    at: event.at,
    key: "job-event-" + event.id,
    title: [heading, ...identity].filter(Boolean).join(" · "),
    content: [
      fields([["Estado", label(states, event.state)], ["Motivo", record.reason],
        ["SHA", sha(record.sha || record.candidate || stage?.input_sha)]]),
      stage ? [
        fields(identityFields(stage)), anchor("Ver intento " + value(stage.id), "#attempt-" + stage.id),
      ] : null,
      operations.map(operation => operationCard(operation, event.state)),
      event.path_approvals?.length ? textList(event.path_approvals.map(path =>
        [value(path.path), value(path.operator), when(path.at)].join(" · "))) : null,
      feedbackRecord(event.feedback), checkReport(event.checks),
      event.repair ? [
        fields([["Encaminamiento", event.repair.decision], ["SHA del fallo", sha(event.repair.sha)]]),
        event.repair.diagnostics?.map(diagnostic),
      ] : null,
      event.ci ? fields([["Estado de CI", event.ci.state], ["SHA de CI", sha(event.ci.sha)]]) : null,
      event.ci_poll_error ? fields([["Error de sondeo de CI", event.ci_poll_error.message]]) : null,
    ],
  };
}

function pullEvent(event) {
  const record = event.detail || {};
  const before = record.before?.sha || record.before;
  const after = record.after?.sha || record.after;
  return {
    at: event.at, key: "pull-event-" + event.id,
    title: label(eventNames, event.kind) + (record.name ? " · " + record.name : ""),
    content: [
      fields([["SHA", sha(event.sha)], ["Origen", record.source], ["Conclusión", record.conclusion],
        ["Intento de CI", record.attempt], ["Error de observación", record.error?.message],
        ["Anterior", typeof before === "object" ? null : before],
        ["Observado", typeof after === "object" ? null : after]]),
      diagnostic(record.diagnostic), github("Ver resultado en GitHub", record.html_url || record.details_url || record.url),
    ],
  };
}

function timeline(events) {
  // Sort across independent streams. Stable sorting preserves source order on ties.
  const latest = [...events].sort((left, right) => (Number(dateValue(right.at)) || 0) - (Number(dateValue(left.at)) || 0));
  return node("section", [
    node("h2", "Cronología"),
    nonempty(latest.length ? [node("ol", latest.map(event => node("li",
      disclosure(when(event.at) + " · " + event.title, event.content, event.key))), { class: "timeline" })] : [],
      "Todavía no hay eventos registrados."),
  ], { class: "section", id: "timeline" });
}

function repairPanel(job) {
  const repair = job.repair;
  if (!repair && !job.ci_poll_error && !job.ci) return null;
  return section(repair ? "Reparación de CI" : "CI registrado por el controlador", [
    fields([["Estado de CI", job.ci?.state], ["Decisión de reparación", repair?.decision],
      ["SHA que produjo el fallo", sha(repair?.sha)], ["Candidato en verificación", sha(job.candidate_sha)],
      ["Etapa de destino o en curso", label(roles, job.intervention.retry_stage || job.stage)],
      ["Agente o ejecutor activo", job.active_attempt?.executor],
      ["Proveedor activo", job.active_attempt?.provider],
      ["Modelo efectivo", job.active_attempt?.model?.effective],
      ["Próximo sondeo", when(job.retry_at)]]),
    job.ci_poll_error ? fields([["Error al sondear", job.ci_poll_error.message]]) : null,
    repair?.diagnostics?.map(diagnostic),
    job.ci?.checks?.length ? disclosure("Resultados observados por el controlador",
      node("ul", job.ci.checks.map(check => checkRow(check, job.ci.sha, null)), { class: "rows" })) : null,
  ], "panel");
}

function jobPage({ job, attempts, events, ci }) {
  const allAttempts = new Map(attempts.map(record => [record.id, record]));
  for (const event of events) {
    for (const record of event.historical_stages || []) {
      if (!allAttempts.has(record.id)) allAttempts.set(record.id, record);
    }
  }
  if (job.active_attempt && !allAttempts.has(job.active_attempt.id)) allAttempts.set(job.active_attempt.id, job.active_attempt);
  const recent = [...allAttempts.values()].reverse();
  const providers = new Set(recent.map(record => record.provider).filter(Boolean));
  const previousFor = stage => allAttempts.get(stage.previous_attempt) ||
    recent.slice(recent.indexOf(stage) + 1).find(record => record.role === stage.role);
  const active = recent.find(record => record.id === job.active_attempt?.id);
  const jobEvents = events.map(event => jobEvent(event, allAttempts));
  const ciEvents = (ci?.events || []).map(pullEvent);
  return [
    node("a", "Volver al listado", { class: "back", href: route() }),
    node("p", job.slug + " · Issue #" + job.number, { class: "meta" }),
    node("div", [node("h1", value(job.title, "Trabajo #" + job.number)),
      badge(label(states, job.state), categories[job.state])], { class: "title-row" }),
    node("div", [github("Issue en GitHub", job.issue?.url), github("PR #" + job.pr?.number + " en GitHub", job.pr?.url),
      ci ? anchor("Detalle del PR observado", route({ view: "pulls", pull: job.repo_id + "/" + job.pr.number })) : null],
    { class: "links" }),
    section("Estado del trabajo", fields([
      ["Etapa actual", label(roles, job.stage)], ["Intento activo", job.active_attempt?.id],
      ["Último intento", job.last_attempt_id], ["Motivo registrado", job.reason], ["Proveedores", [...providers].join(", ")],
      ["SHA base", sha(job.base_sha)], ["SHA candidato", sha(job.candidate_sha)], ["SHA publicado", sha(job.published_sha)],
      ["Rondas consumidas", job.limits?.rounds], ["Límite de rondas", job.limits?.build_limit],
    ]), "panel"),
    receipt ? section("Recibo de la decisión", node("div", [
      node("p", receipt.status === "accepted" ? "Aceptada · pendiente de aplicación"
        : label(decisionResults, receipt.status), { role: "status" }),
      fields([["Recibo", receipt.id], ["Operador", receipt.operator], ["Decisión", label(decisionNames, receipt.kind)],
        ["Aceptada", when(receipt.accepted_at)],
        ...(receipt.applied_at ? [["Aplicada por el controlador", when(receipt.applied_at)],
          ["Estado reanudado", label(states, receipt.state)]] : []),
        ...(receipt.rejected_at ? [["Rechazada el", when(receipt.rejected_at)],
          ["Motivo del rechazo", receipt.error?.message]] : []),
        ["Estado previsto", receipt.status === "accepted" ? label(states, receipt.resume_state) : null],
        ["Estado observado", label(states, job.state)]]),
    ]), "panel") : null,
    interventionPanel(job, allAttempts.get(job.intervention.failed_attempt || job.last_attempt_id)),
    section("Recuperaciones automáticas", [
      fields([["Presupuesto utilizado", job.recovery?.used], ["Límite autorizado", job.recovery?.limit],
        ["Construcciones de recuperación", job.recovery?.builds],
        ["Próxima etapa automática", label(roles, job.recovery?.stage)],
        ["Próximo intento", job.recovery?.next_at ? when(job.recovery.next_at) : null]]),
      job.recovery?.history?.length ? node("ol", job.recovery.history.map(item => node("li", [
        node("strong", label(recoveryNames, item.action)),
        fields([["Fecha", when(item.at)], ["Motivo", item.reason], ["Candidato", sha(item.candidate)],
          ["Base", sha(item.base)]]), item.paths.length ? textList(item.paths) : null,
      ]))) : null,
    ], "panel"),
    !job.intervention.pending && submission ? section("Envío de decisión", actionPanel(job), "panel") : null,
    active ? section("Etapa activa", attemptCard(active, job, previousFor(active))) : null,
    repairPanel(job),
    job.merge_evaluation || job.merge_intervention || job.merge_result ? section("Integración del PR", [
      fields([["Base evaluada", sha(job.merge_evaluation?.binding?.base)],
        ["Head evaluado", sha(job.merge_evaluation?.binding?.head)],
        ["Revisor", job.merge_evaluation?.reviewer?.provider],
        ["Modelo", job.merge_evaluation?.reviewer?.model?.effective || job.merge_evaluation?.reviewer?.model?.requested],
        ["Política", job.merge_evaluation?.binding?.policy],
        ["Veredicto del revisor", job.merge_evaluation?.result?.decision],
        ["Decisión del controlador", job.merge_evaluation?.decision],
        ["Motivo", job.merge_evaluation?.result?.summary],
        ["Siguiente paso", job.merge_intervention?.next_action],
        ["Resultado observado", job.merge_result?.status],
        ["Commit integrado", sha(job.merge_result?.merged_sha)]]),
      textList(job.merge_intervention?.reasons || job.merge_evaluation?.reasons || []),
      job.merge_evaluation?.result ? disclosure("Evidencia y pruebas", [
        textList(Object.entries(job.merge_evaluation.ci).map(([ref, check]) =>
          ref + " · " + check.name + " · " + check.conclusion)),
        textList(job.merge_evaluation.result.missing_tests),
      ]) : null,
    ], "panel") : null,
    ci ? section("CI e historial de SHAs", ciHistory(ci, job)) : null,
    job.checks ? section("Última comprobación local", checkReport(job.checks), "panel") : null,
    timeline([...jobEvents, ...ciEvents]),
    section("Historial de intentos", nonempty(recent.filter(stage => stage !== active).map(stage =>
      attemptCard(stage, job, previousFor(stage))), "Aún no hay intentos anteriores registrados.")),
  ];
}

function pullPage(data) {
  const { pull, events } = data;
  return [
    node("a", "Volver a PR y CI", { class: "back", href: route({ view: "pulls" }) }),
    node("h1", pull.slug + " · PR #" + pull.number),
    node("div", [
      github("PR en GitHub", pull.url),
      pull.job?.number ? anchor("Trabajo #" + pull.job.number,
        route({ job: pull.repo_id + "/" + pull.job.number })) : null,
    ], { class: "links" }),
    section("Estado de CI", ciHistory(data, data.job)),
    timeline(events.map(pullEvent)),
  ];
}

function render(data) {
  if (jobID) return jobPage(data);
  if (pullID) return pullPage(data);
  if (selected === "pulls") return pullList(data);
  return jobsPage(data);
}

let lastView = null;
let currentData = null;
let pending = false;
let started = false;
let refreshAgain = false;

function focusKey(item) {
  if (item.matches("a")) return "link:" + item.getAttribute("href");
  if (item.matches("select, input, textarea, button") && item.name) return item.tagName + ":" + item.name;
  if (item.matches("summary")) return "summary:" + item.parentElement.id + ":" + item.textContent;
  return item.tagName;
}

const focusable = "a[href], button, select, summary, input, textarea";

function paint() {
  if (currentData) replaceView(currentData);
}

function replaceView(data) {
  const snapshot = JSON.stringify([data, actionContext, actionNotice, actionError, sending,
    Boolean(submission), Boolean(credential), draft.kind, draft.options.role, receipt]);
  if (snapshot === lastView) return;
  const focused = document.activeElement;
  const key = main.contains(focused) && focused.matches(focusable) ? focusKey(focused) : null;
  const token = main.querySelector('input[name="token"]')?.value;
  const selection = key && focused.matches("textarea") ? [focused.selectionStart, focused.selectionEnd] : null;
  const focusGroup = key && focused.closest("[id]")?.id;
  const selections = new Map([...main.querySelectorAll(".filters select")].map(input => [input.name, input.value]));
  const disclosures = new Set([...main.querySelectorAll("details[open][id]")].map(item => item.id));
  main.replaceChildren(...render(data).flat(Infinity).filter(item => item != null));
  for (const item of main.querySelectorAll("details[id]")) item.open = disclosures.has(item.id);
  for (const input of main.querySelectorAll(".filters select")) {
    const previous = selections.get(input.name);
    if ([...input.options].some(option => option.value === previous)) input.value = previous;
  }
  const tokenInput = main.querySelector('input[name="token"]');
  if (token && tokenInput) tokenInput.value = token;
  if (key) {
    const group = document.getElementById(focusGroup) || main;
    const replacement = [...group.querySelectorAll(focusable)].find(item => focusKey(item) === key);
    (replacement || main).focus({ preventScroll: true });
    if (replacement && selection) replacement.setSelectionRange(...selection);
  }
  if (lastView === null && /^#[\w-]+$/.test(location.hash)) {
    document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }
  lastView = snapshot;
}

async function refresh(manual = false) {
  if (pending) {
    if (manual) refreshAgain = true;
    return;
  }
  pending = true;
  main.setAttribute("aria-busy", "true");
  if (!started) notice.textContent = "Cargando datos…";
  if (manual && started) notice.textContent = "Actualizando datos…";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const data = await read(controller.signal);
    if (data.actions && data.actions.revision !== actionContext?.revision) {
      draft.confirmed = false;
      draft.options = {};
    }
    actionContext = data.actions ?? null;
    const pendingReceipt = actionContext?.pending_receipt;
    if (pendingReceipt && submission === null
        && (receipt?.id !== pendingReceipt.id || receipt.status === "accepted")) {
      receipt = pendingReceipt;
    }
    currentData = data;
    paint();
    freshness.textContent = "Última actualización · " + dateFormatter.format(new Date());
    notice.textContent = "";
    notice.classList.remove("error");
  } catch (error) {
    controller.abort();
    notice.textContent = error.status === 404 && detail ? "El trabajo o PR ya no está disponible."
      : "No se pudo leer el servicio de observación. Usa Actualizar para volver a intentarlo.";
    if (lastView !== null) notice.textContent += " Se conserva la última lectura.";
    notice.classList.add("error");
  } finally {
    clearTimeout(timeout);
    started = true;
    pending = false;
    main.setAttribute("aria-busy", "false");
    if (refreshAgain) {
      refreshAgain = false;
      refresh();
    }
  }
}

function refreshAfterPending() {
  if (pending) refreshAgain = true;
  else refresh();
}

nav();
refreshButton.addEventListener("click", () => refresh(true));
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 10000);
