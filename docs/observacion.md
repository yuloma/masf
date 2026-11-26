# API local de observación v1

`masf observe-api` sirve el registro, la cola y la evidencia de la instalación existente en `127.0.0.1:8765`. Usa `--port` para elegir otro puerto entre 1 y 65535. Necesita una `queue.sqlite3` inicializada por el controlador en `$XDG_STATE_HOME/masf` (por defecto `~/.local/state/masf`) y el TOML de la instalación.

## Arranque nativo

```bash
masf --config /ruta/privada/config.toml observe-api --port 8780
```

Consulta el servicio desde el mismo host.

```bash
curl -fsS 'http://127.0.0.1:8780/v1/repositories?limit=20'
curl -fsS 'http://127.0.0.1:8780/v1/jobs?repo_id=7&state=needs_input'
curl -fsS 'http://127.0.0.1:8780/v1/pulls/7/23/ci'
```

El observador necesita lectura del TOML, de la base SQLite y de la evidencia, con permiso de búsqueda en sus directorios. Lee el registro y los límites del TOML directamente, por lo que puede ejecutarse sin los perfiles de checks, los directorios de trabajo, los clientes del proveedor ni credenciales. Cada petición abre una transacción SQLite con `mode=ro` y `query_only`. Una petición posterior ve las escrituras confirmadas del controlador. Si SQLite requiere recuperación tras una interrupción, arranca el controlador sobre el estado con escritura para que la complete.

## Despliegue en Docker

Usa el [Compose independiente](../examples/docker/observer.yaml) en Docker Engine sobre Linux. La red de host permite que el contenedor sirva en el loopback del host. Utiliza la imagen del controlador ya disponible en el Engine local.

```bash
export MASF_OBSERVER_CONFIG=/ruta/privada/config.toml
export MASF_STATE_DIR=/ruta/de/estado/masf
export MASF_UID="$(id -u)" MASF_GID="$(id -g)"
export MASF_OBSERVER_PORT=8780
docker compose --env-file /dev/null -p masf-observation -f examples/docker/observer.yaml config --quiet
docker compose --env-file /dev/null -p masf-observation -f examples/docker/observer.yaml up -d
curl -fsS 'http://127.0.0.1:8780/v1/jobs'
```

Selecciona el UID y GID con lectura de los datos. Si la instalación mantiene la evidencia privada al UID del controlador, usa ese UID con los montajes de solo lectura. El observador monta el archivo TOML y el directorio de estado, incluidos los archivos auxiliares de SQLite, en solo lectura. El sistema de archivos del contenedor también se monta en solo lectura. El manifiesto prescinde de los directorios de trabajo, el socket Docker, los secretos de proveedores y el token de GitHub. Conserva el estado activo de la instalación como origen de las consultas.

## Recursos y respuestas

Usa `GET` y `HEAD` con `Host: 127.0.0.1` o `Host: localhost`, con el puerto cuando corresponda. `HEAD` devuelve los encabezados del mismo recurso sin cuerpo. Las respuestas tienen tipo `application/json` y `Cache-Control: no-store`. Los recursos de colección aceptan `limit` (por defecto 50, máximo 100) y `offset` (por defecto 0). Los filtros exactos se combinan entre sí.

| Ruta bajo `/v1` | Filtros adicionales | Orden de la colección |
| --- | --- | --- |
| `/repositories` | `slug` | Slug en minúsculas |
| `/jobs` | `repo_id`, `slug`, `state` | Repositorio e issue |
| `/jobs/{repo_id}/{issue}` | | |
| `/jobs/{repo_id}/{issue}/events` | `state` | ID de evento |
| `/jobs/{repo_id}/{issue}/attempts` | `role`, `status` | Inserción de intentos y luego evidencia histórica registrada |
| `/jobs/{repo_id}/{issue}/attempts/{attempt_id}` | | |
| `/jobs/{repo_id}/{issue}/artifacts/{artifact_id}` | Paginación para actividad | Orden de actividad en el archivo |
| `/pulls` | `repo_id`, `slug`, `state`, `sha` del head actual, `job_number` | Repositorio y número de PR |
| `/pulls/{repo_id}/{pr}` | | |
| `/pulls/{repo_id}/{pr}/ci` | `sha` | SHA |
| `/pulls/{repo_id}/{pr}/events` | `sha` | ID de evento |

Los filtros de slug y SHA ignoran mayúsculas. Codifica `/` como `%2F` en el valor de `slug`. Las claves de una petición de detalle se toman de las colecciones, con IDs de repositorio e issue/PR numéricos. El registro puede contener repositorios aún sin ID observado. `known_repo_ids` conserva los IDs vinculados al slug por trabajos o PRs y `repo_id` queda en `null` si todavía no hay uno inequívoco. Los trabajos y PR históricos conservan sus claves aunque el slug ya no figure en el registro.

```json
{"version":1,"data":[],"pagination":{"limit":50,"offset":0,"next_offset":null}}
```

En una respuesta de detalle, `data` contiene el objeto y se omite `pagination`. `next_offset: null` indica que la petición agotó la colección. El orden es determinista dentro de cada lectura. Si el controlador cambia los elementos entre dos consultas, un desplazamiento puede apuntar a una posición distinta.

```json
{"version":1,"error":{"code":"not_found","message":"Recurso de observación no encontrado"}}
```

| HTTP | Código |
| --- | --- |
| 400 | `invalid_request` para ruta, consulta o sintaxis HTTP inválida, `invalid_host` para un host ajeno al loopback |
| 404 | `not_found` para recurso ausente, inaccesible o fuera de su ámbito |
| 405 | `method_not_allowed` para otros métodos |
| 413 | `artifact_too_large` para evidencia de más de 2 MiB |
| 500 | `invalid_data` para un registro ilegible |
| 503 | `unavailable` cuando el registro o SQLite no se pueden leer en ese momento |

`null` indica un dato escalar o una entidad opcional que falta. Las colecciones vacías indican que no hay elementos publicables en esa lectura. En los requisitos de CI se conserva `null` cuando no se pudo calcular la lista. Las formas de disponibilidad de los intentos precisan si el dato está `pending` durante ejecución, es `unknown`, o es `not_applicable` al rol. `available` identifica una explicación o identidad registrada, y `reported` indica un modelo efectivo o un uso comunicado por la CLI.

Los campos se proyectan de forma explícita y los textos permitidos se filtran. Se omiten el cuerpo de la issue, configuración interna, reservas, entradas de agentes, argumentos y salidas crudas de comandos. Los enlaces externos se limitan a HTTPS de GitHub y su API, con sus rutas y sin parámetros ni fragmentos.

## Trabajos, intervenciones e intentos

El trabajo agrupa las referencias `issue` y `pr`, `state`, `stage`, `active_attempt`, `last_attempt_id`, los SHAs de base, candidato y publicación, el motivo y el último informe local de `checks`. El informe conserva el SHA, el resultado global y los códigos y duraciones por posición de ejecución. Los informes de otros intentos siguen disponibles en `attempts`.

Para consultar las acciones de #22, usa `repo_id`, `number` (la issue) y la `revision` opaca, junto con `intervention`. Este campo incluye `pending`, motivo, preguntas registradas con sus intentos, etapa de reanudación y conflicto de head, cuando existe. La cronología identifica la etapa resultante en `state` e incluye el feedback, las autorizaciones, operaciones del controlador y resultados registrados. El cliente puede volver a consultar el trabajo y sus eventos después de una decisión. Una intervención resuelta sale de `pending` cuando el controlador mueve el trabajo fuera de `needs_input` y `blocked`.

En conflictos de integración, `merge_observation` describe la observación más reciente del PR y `pr_conflict` contiene su recuperación. `intervention.conflict_action` indica cómo continuar si requiere intervención. `conflict_result` conserva el último resultado y `retry_at` indica cuándo vence una espera programada. La cronología expone observaciones, recuperaciones y resultados con los SHAs de base, head y candidato, rutas afectadas y decisión cuando están disponibles. Los intentos vinculados incluyen `conflict_origin` junto con el agente, proveedor y modelo que consten en la evidencia.

Las respuestas de trabajo y sus eventos exponen estos campos de integración. Cada campo admite `null` cuando falta el registro correspondiente, incluidos los trabajos históricos.

| Campo | Contenido |
| --- | --- |
| `merge_evaluation` | Evaluación versionada con fecha, criterios, rutas, política aplicada y decisión del controlador en `decision`. `reviewer` identifica el intento, proveedor, ejecución y modelo cuando constan. `result` contiene el veredicto del revisor (`auto_merge` o `human_required`), motivos, riesgos, componentes afectados, evidencia, pruebas ausentes y hallazgos bloqueantes. `ci`, `local` y `github_branch_checks` resumen los checks observados. `diff_digest` y `ci_digest` identifican el diff y la evidencia de CI evaluados. |
| `merge_intervention` | Petición de intervención con fecha `at`, motivos en `reasons` y comprobación o decisión pendiente en `next_action`. |
| `merge_intent` | Intención persistida antes de mutar el PR, con `operation` (`ready` o `merge`), `method`, contador `attempts` y fecha `at`. Una intención pendiente puede requerir reconciliación con GitHub. |
| `merge_result` | Último resultado observado en GitHub. `status` indica `ready` al confirmar que el PR está listo, `closed` al cerrar sin merge o `merged` al confirmar el merge con las referencias registradas. `merged_other_head` y `merged_unverified_base` indican que se observó un merge con un head o una base distintos de los registrados, respectivamente. Al observar el cierre incluye `observed_base`, `observed_head` y, si hubo merge y GitHub lo comunica, `merged_sha`. |

El objeto `binding`, cuando está disponible, vincula cada registro con `repo_id`, `pr`, los SHA `base` y `head`, las ramas `base_ref` y `head_ref`, y las huellas `scope` y `policy`. El historial conserva la evidencia de revisiones anteriores. Los consumidores deben usar las referencias vigentes al presentar una decisión como aplicable al PR actual.

La precondición compartida de #22 usa `observation_data.revision(job, last_event_id)` sobre el trabajo persistido sin metadatos temporales de reserva, con el ID máximo de sus eventos o `null`. Una revisión distinta identifica una vista obsoleta. El servicio autenticado de acciones debe comparar ese valor en el ámbito transaccional del controlador antes de aplicar cada decisión, y comprobar además sus permisos y alcance vigentes. El navegador trata la revisión como un valor opaco. Las mutaciones se tramitan en ese servicio y a través de las operaciones del controlador.

Cada intento mantiene su ID y sus SHAs de entrada y de candidato, rol, ejecutor, proveedor, ID de ejecución y fuente, versión de CLI, modelo solicitado y efectivo con su fuente, tiempos, estado y resultado. `usage.measurements` conserva valor, unidad, fuente y, para costes, `kind` igual a `estimated` o `confirmed`. Un cero comunicado se conserva como número. Los checks y las operaciones humanas indican que modelo y uso de CLI no aplican al rol. El mismo `attempt_id` sirve para leer identidad en una etapa activa y consultar después la explicación, con decisiones, criterios, motivo y referencias. Los intentos históricos se adaptan a partir de los registros de SQLite. Si falta el ID original, el adaptador asigna un identificador local estable a la proyección y deja desconocidos el modelo efectivo y los metadatos ausentes.

Cada referencia de explicación contiene `attempt_id`, `artifact_id` y el `sha` de entrada correspondiente. Un `artifact_id: null` indica una referencia a un intento. Usa la ruta de artefactos del mismo trabajo con el ID emitido. El servicio verifica la pertenencia al trabajo, abre archivos regulares dentro de su carpeta de evidencia y aplica las mismas proyecciones antes de devolver `content`. Las lecturas de actividad devuelven los eventos paginados en `data` y describen el artefacto en `artifact`. Una referencia registrada durante una etapa puede responder 404 mientras aún se escribe la evidencia. La API acepta identificadores opacos en las rutas de artefactos.

## PR y CI

El detalle de PR contiene su identidad, head, base, estado, fecha de observación, procedencia y vínculo con el trabajo. Los snapshots de `ci` mantienen la clave de repositorio, PR y SHA. Los checks y workflows conservan su estado, conclusión, identificadores de ejecución, intento y correlación. Los errores de GitHub aparecen en `errors` y en la cronología. Los errores de listado o detalle por repositorio se incluyen en `source_observation`; `changed_at` indica el último cambio de ese registro de errores. La fecha `observed_at` del snapshot indica el último sondeo para ese SHA. `last_success_at` registra la última consulta correcta de cada colección principal, incluso si un sondeo posterior conserva el resultado anterior debido a un error. Las fechas ausentes en snapshots históricos se representan con `null`.

Los fallos incluyen diagnóstico filtrado y las referencias disponibles a logs, anotaciones o detalles de GitHub. El trabajo muestra la última observación usada por el motor en `ci`, el error de su sondeo en `ci_poll_error` y el paso actual en `repair`, con su SHA, decisión, límites, diagnósticos e intervención. Los intentos enlazan la observación causante en `ci_origin` y el resultado en `ci_result`, cuando se registraron. Consulta `/pulls/{repo_id}/{pr}/ci?sha=...` y `/events?sha=...` bajo ese mismo PR para seguir snapshots y eventos de heads anteriores.

Consulta los [contratos de evidencia de etapa y CI](especificacion.md) y la [aceptación de I5](implementacion.md#i5--api-local-de-observación).

## API autenticada de acciones v1

El modo nativo con `--actions-token-file` añade `GET`, `HEAD` y `POST` en `/v1/jobs/{repo_id}/{issue}/actions` y `GET` y `HEAD` en `/v1/jobs/{repo_id}/{issue}/actions/{id}`. El detalle del trabajo indica su disponibilidad en `actions_enabled`. Las credenciales, el arranque y la aceptación están en la [guía del dashboard](dashboard.md).

Las acciones exigen `Authorization: Bearer TOKEN` y el permiso `intervene`. El servidor valida el host local, el origen cuando está presente y los metadatos de navegación. El `GET` de acciones devuelve la revisión opaca, el operador, el estado, la causa, el SHA candidato, la etapa de reanudación, el número de copias compatibles y las acciones permitidas. Si existe una decisión en espera, `pending` indica que el trabajo tiene un envío pendiente y `pending_receipt` muestra su recibo al operador que lo envió. Cada acción incluye `kind` y `resume_state`, con parámetros y contexto según corresponda. El contexto protegido contiene los archivos exactos y un resumen de cambios. El campo `build_timeout` informa de la etapa, el plazo agotado y las rondas consumidas y autorizadas. Las opciones de recuperación incluyen referencias opacas a las copias compatibles del propio trabajo. Para un conflicto de base devuelve el candidato, la base registrada, las rutas, el prefijo de referencia local y los commits válidos si existen. El detalle del trabajo expone el estado y el historial de recuperación en `recovery`, con consumo, límite, construcciones, etapa y próximo intento.

El `POST` exige `Content-Type: application/json` y una cabecera `Idempotency-Key` de 16 a 128 caracteres alfanuméricos, guiones o guiones bajos. Este ejemplo usa la revisión devuelta por el servicio.

```json
{"kind":"answer","revision":"v1:REVISION","evidence":"Para Ana","confirm":true,"options":{}}
```

| `kind` | `options` |
| --- | --- |
| `answer`, `confirm_scope`, `resolve` | `{}` |
| `allow_paths` | `{"paths":[".github/workflows/factory.yml"]}`, con la lista exacta ofrecida |
| `assign_provider` | `{"role":"plan","provider":"cursor"}`, con un proveedor ofrecido para el rol |
| `add_rounds` | `{"extra":1}` |
| `add_recoveries` | `{"extra":2}` |
| `resolved_commit` | `{"sha":"SHA"}`, con un commit ofrecido |
| `accept_head` | `{"sha":"SHA"}`, con el head observado y `extra` opcional para ampliar rondas |
| `reuse_workspace` | `{"reference":"REFERENCIA"}`, con una referencia ofrecida |
| `extend_timeout` | `{"seconds":1200,"reference":"REFERENCIA"}`, con una referencia ofrecida |
| `retry_merge_review` | `{}` |
| `record_manual_merge` | `{}` |

Cuando hay una petición de integración, el `GET` de acciones devuelve `merge_intervention`. `retry_merge_review` vuelve a `pr_open`, descarta la evaluación y la autorización registradas y reanuda la observación antes de una nueva evaluación. `record_manual_merge` se ofrece con el `binding` de la petición cuando aún no hay una decisión manual registrada. Al aplicarla, el controlador revalida el PR, las referencias, el alcance y la política y registra el operador, el motivo enviado en `evidence` y el `binding` en el historial. El trabajo permanece en `needs_input` para que el operador integre el PR en GitHub con las revisiones autorizadas. Un cambio posterior de base, head, alcance o política invalida esa autorización.

El POST devuelve HTTP 202 al persistir la aceptación. El recibo en `data` incluye `id`, `operator`, `kind`, `status`, `accepted_at`, `revision`, `origin`, `repo_id`, `number` y `resume_state`. El GET del recibo devuelve HTTP 200 y exige la credencial del operador que lo envió.

| `status` | Significado |
| --- | --- |
| `accepted` | La decisión está persistida y espera al controlador. El trabajo conserva el estado y la revisión de la intervención. |
| `applied` | El controlador persistió el recibo, el evento y la transición en una transacción. El recibo incluye `applied_at` y el `state` al que se reanudó el trabajo. |
| `rejected` | La revalidación impidió la aplicación. El recibo incluye `rejected_at` y `error` con el código y un mensaje. |

Un reenvío con la misma clave y contenido devuelve el mismo ID. El estado del recibo determina si el POST repetido responde 202 o 200. Si el transporte falla, reenvía el mismo contenido con la misma clave. Tras un conflicto de revisión, vuelve a leer el trabajo y confirma el contexto con una nueva clave. El controlador revalida el contexto antes de aplicar y continúa mediante su despacho habitual. Los eventos de aceptación y resultado comparten el ID del recibo. Sigue el trabajo en `/v1/jobs/{repo_id}/{issue}`, `/events` y `/attempts`.

| HTTP | Código |
| --- | --- |
| 400 | `invalid_request` para solicitudes mal formadas o `invalid_host` para un host ajeno |
| 401 | `authentication_required` |
| 403 | `invalid_origin` o `insufficient_permissions` |
| 404 | `not_found` para un trabajo o recibo inaccesible |
| 409 | `revision_conflict` para contexto obsoleto, `decision_in_progress` para una reserva activa o una decisión pendiente, `idempotency_conflict` para una clave ligada a otro envío, `incompatible_state` para un estado que no admite intervención |
| 422 | `invalid_decision` para acciones o valores fuera de las opciones vigentes |
| 503 | `unavailable` para una validación o escritura temporalmente inaccesible |

Con el dashboard abierto en loopback, este ejemplo se puede ejecutar en la consola del navegador. Cambia los IDs y usa una acción ofrecida por el servicio. Ante un error de transporte, vuelve a llamar a `sendDecision()` en la misma pestaña. Conserva la credencial en memoria durante la consulta y recarga la pestaña al terminar.

```javascript
const endpoint = "/v1/jobs/123/1/actions";
const headers = { Authorization: "Bearer " + prompt("Token del operador") };
const context = await fetch(endpoint, { headers }).then(response => response.json());
const body = JSON.stringify({
  kind: "answer", revision: context.data.revision,
  evidence: "Para Ana", confirm: true, options: {},
});
const idempotencyKey = crypto.randomUUID();
let receipt;
const sendDecision = async () => {
  const response = await fetch(endpoint, {
    method: "POST", body,
    headers: { ...headers, "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
  });
  receipt = await response.json();
  return receipt;
};
const readReceipt = () => fetch(endpoint + "/" + receipt.data.id, { headers })
  .then(response => response.json());
await sendDecision();
```
