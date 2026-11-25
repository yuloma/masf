# Especificación de la fábrica

## Qué debe conseguir el piloto

Una persona etiqueta una issue en un repositorio registrado. MASF la descubre aunque el editor esté cerrado, trabaja sobre una copia independiente y abre un PR en ese mismo repositorio. La persona puede explicar qué se construyó, qué agente intervino en cada etapa y qué comprobaciones pasaron en el commit propuesto. Un segundo repositorio se incorpora a la misma instalación mediante otra entrada de configuración.

El repositorio `yuloma/masf` será la fuente de las versiones del motor, los adaptadores, las instrucciones de cada rol y la instalación. La lista real de proyectos, las credenciales y el historial de trabajos pertenecen a la instalación del operador.

## Cómo se ejecuta

| Componente | Responsabilidad |
| --- | --- |
| Servicio central | Un proceso Python 3.11+ en un contenedor Docker o en Linux nativo sondea GitHub, reclama trabajos y publica resultados con Git y `gh`. En la instalación Docker, esas dependencias están en la imagen del controlador. |
| Registro de repositorios | El operador configura `owner/repo`, etiqueta, rama base opcional, entorno, preparación, checks y rutas protegidas. Toda selección se limita a repositorios habilitados. |
| Entorno de tarea | Un clon dedicado por issue conserva el código entre etapas. Los agentes corren en contenedores desechables. Para los checks se usa una copia limpia del SHA en una imagen o un proyecto Compose con los servicios que necesite ese repositorio. |
| Adaptadores | Inician una CLI por etapa, aplican sus permisos y traducen la respuesta al contrato común. El modelo y el ejecutable pueden variar por proveedor. |
| Publicación | Solo el servicio tiene la credencial de escritura en GitHub. Tras los checks y la revisión, sube la rama y abre un PR en borrador con un enlace a la issue. El merge corresponde a una persona. |

Para el primer host, SQLite sirve a la vez de cola y registro de estado. Una instalación persistente puede dar servicio a varios repositorios con un único trabajo activo. El intervalo de sondeo y la concurrencia son configurables. Los webhooks, los workers en varias máquinas y el panel web pueden evaluarse cuando el uso lo justifique.

La configuración toma como base los roles globales y aplica después las excepciones de `repositories.routing`. Cada repositorio puede indicar `runner_image` para usar una imagen distinta de `runner.image`. Un campo `model` opcional en cada proveedor fija el modelo solicitado. Si se omite, se registra el modelo efectivo cuando lo comunique la CLI. Una ruta a un proveedor desconocido o desactivado en un destino habilitado es un error de configuración. Se puede operar aunque falte una CLI sin roles asignados. [Ejemplo TOML](../examples/factory.toml).

El controlador en Docker es el despliegue recomendado para el piloto. SQLite y los directorios de trabajo persisten al recrear su contenedor. El bloque opcional `repositories.compose` selecciona los manifiestos aprobados y el servicio que ejecuta `setup` y `checks`. La imagen de los agentes sigue determinada por `runner_image` o `runner.image`. El controlador nativo admite los mismos dos entornos de comprobación. [Montajes, permisos y ciclo de verificación](docker.md).

## Ciclo de una issue

La unidad de trabajo es `(id numérico del repositorio de GitHub, número de issue)`. La admisión exige una issue abierta con la etiqueta elegida, por ejemplo `masf:ready`. Se puede reutilizar una etiqueta que ya exista. El sondeo recorre todas las páginas y descarta las entradas que sean PR. En la admisión se guarda el título y el cuerpo como datos, su huella, la configuración efectiva, la rama base y su SHA. Los permisos del agente y los comandos ejecutables proceden de la configuración del operador.

| Estado | Trabajo y condición de salida |
| --- | --- |
| `queued` → `planning` | Se reclama el trabajo. El rol `plan` concreta el alcance autorizado y los criterios observables. Si hace falta recortar el encargo o decidir algo que cambia el resultado, guarda la pregunta y pasa a `needs_input`. Con un plan suficiente se inicia `building`. |
| `building` | El rol `build` recibe plan, criterios y, a partir de la segunda ronda, los fallos y hallazgos anteriores. Entrega cambios en el clon. El servicio registra el commit candidato. |
| `checking` | El servicio comprueba rutas protegidas y ejecuta los checks sobre ese commit. Un fallo funcional devuelve el trabajo a `building` si queda una ronda. Un cambio en una ruta protegida lleva a `blocked`. El operador puede autorizar archivos exactos para ese trabajo mediante `resume --allow-protected-path RUTA`. El permiso queda registrado y vinculado a la huella vigente de la issue. |
| `reviewing` | El rol `review` lee el diff, los criterios y la evidencia de checks del mismo SHA. Devuelve `pass` o `changes_requested` con hallazgos concretos. Se permite otra ronda dentro del límite. |
| `publishing` → `pr_open` | Con checks y revisión favorables para el SHA vigente, el servicio publica una rama `masf/issue-<n>` y abre o recupera su PR. La descripción incluye criterios, resultado de checks y `Closes #<n>`. |
| `retry_wait` | Un timeout, una salida incompleta o un fallo temporal reconocido conserva la etapa, SHA y evidencia. El servicio espera hasta la fecha persistida y vuelve a reclamar el trabajo. El plazo de GitHub tiene prioridad sobre la espera exponencial. |
| `needs_input` / `blocked` | El operador ve el motivo mediante `status`. Se requiere intervención para preguntas, acceso denegado, rutas protegidas, errores no recuperables o presupuesto agotado. `resume` reevalúa la elegibilidad y permite continuar tras atender la causa. |
| `merged` / `closed` / `cancelled` | El servicio refleja un PR fusionado o cerrado. Si la issue se cierra o pierde la etiqueta antes de publicar, cancela el trabajo pendiente. |

Cada intento de construcción cuenta en `rounds`. `max_build_rounds` limita las rondas funcionales y `max_recovery_attempts` autoriza intentos adicionales de recuperación. Los intentos adicionales figuran también en `recovery_builds`. El presupuesto de recuperación es compartido por trabajo, persiste tras reinicios y comprende reintentos, actualizaciones de base y resolución de conflictos. Su valor predeterminado es dos y cero lo desactiva. La espera exponencial parte de `recovery_backoff_seconds`, treinta segundos por defecto. Un reinicio durante una construcción de recuperación también consume presupuesto al continuarla.

El agente implementador dispone de escritura sobre los archivos de la tarea. Planificación y revisión reciben el código de solo lectura. El servicio guarda los resultados fuera del clon. Un cambio del SHA invalida los checks y la revisión. Si avanza la rama base antes de publicar, se actualiza la base y se repiten las verificaciones sobre el nuevo candidato. Un conflicto vuelve al implementador con el árbol combinado y las rutas afectadas. El commit resuelto incluye la base y el candidato anterior como padres, no puede conservar marcadores de conflicto y pasa de nuevo por rutas protegidas, checks y revisión. Si el servicio agota su presupuesto o no puede continuar dentro del alcance y permisos vigentes, deja el trabajo en `blocked`.

Antes de publicar se releen el estado, la etiqueta y la huella del encargo. Si el autor cambió el contenido, el trabajo pasa a `needs_input` para confirmar el alcance actualizado. Las reglas o instrucciones de proyecto aplicables, incluido un `AGENTS.md` existente, se ponen a disposición de los tres adaptadores. Las convenciones del proyecto orientan la implementación dentro de los permisos fijados por el operador.

## Observación de PR y CI

`serve` admite issues y recorre las páginas de PR abiertos de cada repositorio habilitado mientras los workers ejecutan agentes y checks locales. El intervalo sigue `service.poll_seconds` y se amplía si GitHub comunica una espera de cuota. Un fallo aislado permite seguir consultando otros PR o repositorios mientras haya cuota. Si un PR registrado desaparece de un listado completo, el controlador consulta su detalle. El trabajo publicado pasa a `closed` o `merged` cuando el detalle confirma el resultado.

El vínculo con un trabajo se resuelve por la referencia ya registrada o por la marca de publicación contrastada con el repositorio, las ramas y el SHA. Los PR sin vínculo aparecen con `job: null`. El operador puede asociarlos a una issue ya registrada del mismo repositorio con `masf link-pr OWNER/REPO#PR OWNER/REPO#ISSUE`. Esta asociación es local y se puede repetir para la misma pareja. La referencia publicada del trabajo tiene prioridad.

El controlador consulta check runs con todas sus ejecuciones y commit statuses por el SHA de `head`. Consulta también los workflows de Actions del mismo SHA y los jobs de cada intento disponible. Correlaciona jobs y checks por su ID y conserva la vinculación al workflow cuando coincide el ID de la suite. La protección de la rama base y las reglas aplicables aportan los nombres obligatorios y su aplicación de GitHub, cuando el acceso permite leerlos. Una ejecución pendiente, en cola o activa solo modifica la observación del CI. El SHA del candidato y las rondas del trabajo conservan la evidencia de construcción local.

Un check con error puede aportar anotaciones, salida del check y un extracto del log del job. Antes de guardar los fragmentos se eliminan los escapes de terminal y se filtran los patrones de credenciales y los valores marcados con `::add-mask::` que aparezcan en el log. La referencia al log completo apunta al endpoint estable de GitHub, que exige el acceso correspondiente. Las denegaciones de acceso se registran por recurso y los diagnósticos pendientes vuelven a intentarse en sondeos posteriores de ese head mientras el PR siga abierto. Para integraciones externas se conserva el enlace comunicado por el check o commit status.

### Formato de observación v1

`status --pr OWNER/REPO#N` devuelve objetos JSON, uno por línea. `status` conserva las filas de trabajos y añade las observaciones en ese mismo formato. `logs --pr OWNER/REPO#N` consulta el historial y `--sha SHA` lo limita a una revisión. La versión de este formato es `version: 1` en la observación del PR. La trazabilidad de los trabajos y las interfaces de #15 y #16 pueden consultar las siguientes entidades locales.

| Entidad | Clave y contenido |
| --- | --- |
| `pull_request` | `(repo_id, number)` identifica el PR de forma estable. `id` y `url` proceden de GitHub. `state` es `open`, `closed` o `merged`. `base` y `head` incluyen `repo_id`, `repo`, `ref` y `sha`. Incluye `draft`, `observed_at`, `job`, `provenance`, `errors` y el `ci` del head. |
| `job` | Incluye `number` de issue, `candidate`, `rounds` y `method` con valor `registered`, `publication` u `operator`. `provenance` distingue `candidate`, `unknown`, `different_head` y `merge_from_base`. En el último caso se verifica la ascendencia con los padres del commit y se registran la rama base, el commit integrado y la fuente. |
| `pull_evidence` | `(repo_id, number, sha)` identifica un snapshot `ci` con `checks`, `runs`, `required`, `base_ref` y `errors`. Los snapshots de SHA anteriores permanecen disponibles. |
| `checks` | Mapa por origen e ID con prefijo `check:`, `status:` o `job:`. Cada valor tiene `status` de GitHub, `phase` normalizada, la conclusión, `current`, enlaces e identificadores de correlación. Si GitHub reutiliza un ID de check para otro intento conocido, se archiva el anterior con su job e intento en la clave. Los commit statuses anteriores del mismo contexto conservan `current: false`. |
| `runs` | Mapa por `run_id:attempt`, con `unknown` en la clave si falta el intento. Guarda el ID y enlace del workflow, la ejecución, su intento y su lista de jobs. `historical: true` identifica intentos anteriores de la misma ejecución. Si falla la lectura del detalle de un intento anterior, se conserva su último estado válido y se reintenta la consulta. Una nueva ejecución del mismo workflow recibe su propia clave. |
| `required` | `known` indica lectura completa de los requisitos de protección y reglas. `checks` contiene los pares `name` y `app_id` observados, incluso si una consulta posterior falla. `missing` enumera los requisitos sin resultado y `unverified` los que tienen un nombre coincidente con aplicación sin confirmar. Si falla la lectura de checks o statuses, se conserva el cálculo anterior del mismo SHA si existe. |
| `pull_event` | Evento con ID local, fecha, PR, SHA, tipo y detalle. Registra descubrimiento, cambios de PR, transiciones de fase, conclusiones, intervención, diagnóstico y cambios en errores de consulta. Si un check se correlaciona con un job ya observado, `detail.previous_key` permite seguir sus eventos previos. El snapshot y sus eventos se escriben en la misma transacción. |
| `pull_source` y `pull_source_event` | Error actual e historial de un listado o consulta de identidad o detalle de PR. Sus claves incluyen `slug` y el recurso consultado. Los errores llevan código HTTP si está disponible y un mensaje filtrado. La recuperación deja un evento con `error: null`. |

`phase` toma los valores `pending`, `queued`, `in_progress`, `completed` o `unknown`. Las conclusiones conservan el valor de GitHub, incluidas `failure`, `timed_out`, `cancelled`, `action_required`, `skipped` y `neutral`. Los errores de consulta aparecen en `errors` y los resultados válidos previos siguen visibles. `null` expresa un valor desconocido o aún no comunicado, por ejemplo un intento o el cálculo de checks ausentes sin una lectura válida. Un array vacío señala una lectura que no encontró elementos.

## CI de un PR abierto

La etapa del trabajo y la observación de CI se registran por separado. `status` muestra ambas, el SHA observado, los límites y la intervención pendiente. `status --json` expone el estado y los intentos en un objeto por trabajo. `history OWNER/REPO#N` devuelve las transiciones y los límites de cada intento en JSON por línea, con su evidencia. Los checks locales y la revisión conservan el SHA al que corresponden aunque avance el PR.

| Observación del head vinculado | Decisión |
| --- | --- |
| `waiting` o `pending` | `pr_open` sigue observando. Un workflow en curso, un check pendiente o un rerun en ejecución aplaza la decisión. Las conclusiones del head anterior quedan en el historial. |
| `success` | `pr_open` queda disponible para la decisión de integración del operador. El servicio sigue observando el PR, incluido un rerun posterior. |
| Fallo funcional confirmado | `building` usa el plan vigente y el head fallido. Recibe el check, la ejecución y el intento de CI, junto con evidencia filtrada y acotada. La reserva y el historial identifican una sola reparación para la observación. |
| Fallo de infraestructura | `checking` repite la comprobación local del candidato tras la espera configurada, dentro del límite técnico. Si los checks locales pasan sobre el head fallido, `needs_input` pide relanzar el CI remoto. Un error transitorio del sondeo vuelve a intentar la lectura y respeta además la espera comunicada por GitHub. |
| Acceso, cuota o configuración del CI | `blocked` conserva el diagnóstico y pide resolver la causa y relanzar el CI. |
| Evidencia insuficiente o límite agotado | `needs_input` muestra el check y la acción solicitada. La falta de evidencia requiere consultar el check y relanzar CI con un resultado clasificable. Las rondas funcionales agotadas requieren aprobación explícita. |
| Reparación local aprobada | `publishing` registra el número del PR, el SHA remoto esperado y el SHA aprobado por checks y revisión. El push condicionado actualiza la rama existente. `pr_open` vuelve a observar CI para el nuevo SHA. |
| Head inesperado o alcance modificado | `needs_input` conserva el candidato y muestra la discrepancia. El operador verifica el head o confirma la huella actualizada de la issue. Si el PR se cierra o fusiona, o la issue deja de ser elegible, el trabajo refleja `closed`, `merged` o `cancelled`. |

Se consultan las ejecuciones de GitHub Actions del head y la rama del PR, los check runs de otras integraciones y el último estado por contexto. Los jobs de Actions se toman del intento vigente. La evidencia enlaza los recursos consultados y guarda extractos de anotaciones y logs pertinentes. Un fallo sin señales suficientes requiere intervención. La configuración admite `service.max_ci_technical_retries` (2 por defecto) y `service.ci_retry_seconds` (60 por defecto). Las rondas de reparación funcional cuentan en `max_build_rounds` y se registran también por separado. El checkpoint permite retomar una ronda tras una interrupción y reutilizar el resultado de construcción cuando ya quedó persistido.

Los códigos numéricos de cuota, acceso e infraestructura requieren contexto HTTP explícito, como `HTTP 429`, `status code: 403` o `response code: 503`. Los tiempos de construcción (`4.429`) y los nombres de pruebas aprobadas (`test_rate_limit`) no bastan para bloquear el trabajo. Con esos mensajes incidentales y un `AssertionError`, el diagnóstico sigue siendo funcional y permite reparar dentro del límite de rondas.

### Operaciones del controlador

Ejecuta estos comandos con el TOML y el estado habituales de la instalación. La evidencia indica qué verificó el operador y qué autoriza. Las decisiones quedan asociadas a su identidad local, el trabajo, la huella de la issue y los SHAs correspondientes.

`link-pr PR ISSUE` conserva la asociación local de una observación sin activar una reparación. Para incorporar un PR al ciclo de reparación se usa `link-pr ISSUE --pr N --head SHA --evidence TEXTO`, que valida el alcance y el head antes de registrar el trabajo. `status --json` emite trabajos con sus límites, procedencia e intentos. `status --pr` y `logs --pr` conservan el formato de observación v1.

```bash
masf status --json
masf history 'OWNER/REPO#10'
masf resume 'OWNER/REPO#10' --accept-head SHA --evidence 'Diff y procedencia del nuevo head revisados'
masf resume 'OWNER/REPO#10' --add-rounds 1 --evidence 'Autorizada una ronda para el fallo registrado'
masf resume 'OWNER/REPO#10' --allow-protected-path .github/workflows/factory.yml
masf link-pr 'OWNER/REPO#10' --pr 23 --head SHA --evidence 'Issue, PR y alcance verificados'
```

`--accept-head` requiere el SHA observado en el conflicto y lo contrasta con el PR y su referencia. `--add-rounds` amplía un límite agotado para la huella vigente y puede acompañar la reconciliación del head. Un fallo de CI de acceso, cuota, configuración o evidencia insuficiente se reexamina con `resume` tras atender la causa y solicitar un rerun. Las autorizaciones de rutas siguen siendo por archivo exacto y por trabajo. `link-pr` exige una issue elegible, un PR abierto del mismo repositorio y la rama `masf/issue-N` hacia la base configurada. La vinculación permite iniciar el diagnóstico y solicitar la planificación cuando falte un plan.

## Proveedores y contrato de etapa

| Id en MASF | Interfaz no interactiva de referencia | Tratamiento del resultado |
| --- | --- | --- |
| `codex` | `codex exec --json` | Leer los eventos JSONL y el resultado final. El adaptador puede solicitar un esquema de salida. |
| `cursor` | `agent -p --output-format json` | Comprobar la salida del proceso y extraer el contenido del campo `result` del JSON de Cursor. |
| `claude` | `claude -p --output-format json` | Comprobar el resultado de Claude Code. Si se solicita `--json-schema`, validar la presencia y la forma de `structured_output`. |

Cada trabajo puede usar uno, dos o tres proveedores sin mantener un IDE abierto. Se comprobará la autenticación con la instalación y el plan del operador. Las interfaces están documentadas por los proveedores a 23 de noviembre de 2026. [OpenAI](https://learn.chatgpt.com/docs/non-interactive-mode) · [Cursor](https://cursor.com/docs/cli/headless) · [Formato de Cursor](https://cursor.com/docs/cli/reference/output-format) · [Anthropic](https://code.claude.com/docs/en/headless).

El servicio entrega a cada adaptador el identificador de trabajo e intento, rol, revisión de código, directorio asignado, plazo, permisos y artefactos de entrada. Espera un resultado estructurado y versionado con estado, resumen y los datos propios del rol. Planificación devuelve criterios identificables o una pregunta. Implementación devuelve un resumen y relaciona cada criterio con los cambios. Revisión devuelve un veredicto y sus hallazgos. Los adaptadores convierten el formato del proveedor y validan los campos exigidos antes de avanzar.

Cada etapa recibe sus entradas por contenido o archivos. Un id de sesión del proveedor puede facilitar el diagnóstico, pero otra CLI debe poder continuar solo con el plan, el diff y las evidencias. Una respuesta truncada, un proceso interrumpido o un error de cuota se conservan como fallo. Cambiar de proveedor o ampliar el número de rondas requiere una decisión explícita del operador.

### Formato de evidencia de etapa v1

`status --json` incluye `provenance` al registrar el trabajo y `stage_attempts` con la procedencia vigente al inicio de cada intento. `engine` es la huella SHA-256 de los módulos instalados. `policy` identifica los módulos que determinan admisión, enrutamiento, checks, recuperación y publicación. `configuration` contiene el repositorio efectivo, su base y asignación de roles, los proveedores asignados y los parámetros del servicio. Las huellas funcionan también en una instalación empaquetada. Las instantáneas anteriores se conservan al reconfigurar un proveedor.

| Campo | Contenido |
| --- | --- |
| `id`, `repo_id`, `number`, `lease` | Identificador propio del intento, clave del trabajo y reserva del controlador. |
| `role`, `executor`, `provider`, `execution_id`, `cli_version` | Rol y ejecutor. Los adaptadores capturan el identificador de sesión, hilo o ejecución y su procedencia cuando la CLI los comunica. |
| `input_sha`, `candidate_sha`, `previous_attempt`, `retry_of` | SHA utilizado, candidato producido y vínculos a intentos anteriores. El candidato de una construcción se añade al guardar el commit y se refleja en la transición correspondiente de `history`. |
| `started_at`, `finished_at`, `duration_seconds`, `status`, `result`, `error` | Inicio y cierre UTC, duración monotónica, resultado o fallo. Un intento interrumpido conserva el inicio, lo observado hasta la interrupción y `detected_at`, fecha en que otro controlador detectó la interrupción. |
| `model` | `requested` conserva la selección configurada, incluido un alias. `effective` conserva la identidad comunicada por la CLI con `source`. `availability` toma `reported`, `unknown` o `not_applicable`. |
| `usage` | `availability` toma `reported` o `unknown`. Cada medición incluye nombre, valor, unidad y fuente. Cero cuenta como dato comunicado. Una medición de coste distingue `estimated` de `confirmed`. |
| `explanation` | Resumen breve, decisiones, criterios, referencias a evidencia y motivo de bloqueo o cambio de rumbo cuando el resultado lo indica. `source` indica si deriva del resultado de etapa, del controlador o del operador. `availability` toma `available` o `unknown`. |
| `commands`, `purpose`, `pr`, `ci_origin`, `ci_result` | Comandos de checks o publicación, propósito de los checks, PR asociado y vínculos desde y hacia el CI de una reparación. |

El controlador persiste el inicio en SQLite antes de ejecutar la etapa y actualiza los metadatos de proveedor al recibirlos. `history` emite `stage_event` con `started`, `finished` o `interrupted` y el registro de ese momento. Los checkpoints y transiciones enlazan por ID las decisiones posteriores de reintento, confirmación o bloqueo. Los archivos `evidence/<repo_id>-<number>/<role>-<id>.json` contienen el resultado de la etapa y los `*.activity.jsonl` la actividad disponible. Para checks, el intento referencia el informe `checks.json` del SHA. Para publicación, los eventos conservan la intención, el intento que la registró y el PR y SHA confirmados. Las autorizaciones humanas incluyen ejecutor local, decisión, fecha y SHA, con modelo `not_applicable`.

La explicación se construye a partir del resumen y los campos del resultado validado. Usa los criterios del plan en construcción y revisión, los hallazgos de revisión y la evidencia del intento. Un fallo de ejecución comunica la causa que observó el controlador. En los registros históricos se adaptan los resultados y errores conservados. `null` identifica los datos desconocidos y `legacy_model` conserva el campo antiguo cuando se desconoce si era una selección o un dato comunicado. SQLite amplía el esquema al abrirlo y permite reutilizar el plan, candidato y checks históricos vigentes.

Estos fragmentos muestran una revisión activa, su posible resultado y dos lecturas de fallos.

```json
{"id":"intento-22","role":"review","provider":"cursor","input_sha":"abc123","status":"active","cli_version":"2026.11.11","model":{"requested":"auto","effective":null,"source":null,"availability":"unknown"},"explanation":{"availability":"unknown"}}
{"id":"intento-22","role":"review","provider":"cursor","input_sha":"abc123","status":"completed","model":{"requested":"auto","effective":"modelo-2026-11","source":"cursor:cli:effective_model","availability":"reported"},"usage":{"availability":"reported","measurements":[{"name":"outputTokens","value":0,"unit":"tokens","source":"cursor:cli:usage.outputTokens"}]},"explanation":{"availability":"available","summary":"Revisión aprobada","decisions":["pass"],"criteria":["AC1 Conservar los checks del SHA"],"evidence":["review-intento-22.json","intento-checks-21"],"reason":null,"source":"stage_result"}}
{"id":"intento-23","role":"review","status":"failed","cli_version":null,"error":"Falló la consulta de versión","model":{"availability":"unknown"},"explanation":{"availability":"available","reason":"Falló la consulta de versión"}}
{"id":"intento-24","role":"review","status":"failed","cli_version":"2026.11.11","execution_id":"sesion-12","error":"Salida incompleta","model":{"requested":"auto","effective":"modelo-2026-11","source":"cursor:cli:effective_model","availability":"reported"}}
{"historical":true,"role":"review","input_sha":"abc123","legacy_model":"auto","model":{"effective":null,"availability":"unknown"},"provenance":{"engine":null,"policy":null,"configuration":null},"explanation":{"availability":"available","summary":"Revisión aprobada"}}
```

Un fallo de CI se identifica por PR, SHA y la clave de sus resultados fallidos. La decisión guarda clasificación, errores, evidencia y motivo. `ci_origin.id` identifica esa decisión y `ci_result` permite consultar el CI del candidato en el formato de observación por PR y SHA. `kind: "ci_observation"` distingue las actualizaciones de evidencia dentro del mismo estado del trabajo.

```json
{"id":"intento-25","role":"build","provider":"codex","input_sha":"abc123","candidate_sha":"def456","ci_origin":{"pr":23,"sha":"abc123","key":[[71,2,205,"failure"]],"id":"huella-del-fallo"},"ci_result":{"pr":23,"sha":"def456"}}
```

```bash
masf --config FILE status --json
masf --config FILE history OWNER/REPO#1
masf --config FILE logs --pr OWNER/REPO#23 --sha def456
```

SQLite, los archivos de evidencia y los resúmenes publicados filtran los campos y textos antes de serializar. El controlador usa la configuración privada para ejecutar los checks. Los perfiles Compose necesarios para limpiar una ejecución interrumpida se conservan en el directorio privado de trabajos y se retiran tras la limpieza.

## Persistencia, aislamiento y recuperación

- **Un trabajo y un PR por issue.** SQLite impone la clave única y asigna cada intento a un worker en una transacción. La reserva tiene vencimiento y renovación. El servicio acepta escrituras y autorizaciones de publicación solo del intento vigente. Tras una caída reconcilia primero los artefactos, la rama y el PR, incluso si GitHub ya creó el PR y aún falta guardar su número.
- **Publicación recuperable.** Se registra la intención antes del push o la creación del PR. Se busca por repositorio, rama, base y marca del trabajo en el PR. Si la rama ya existe y su procedencia o SHA son inesperados, se pide intervención. El servicio conserva los cambios de terceros.
- **Permisos por operación.** El diagnóstico distingue lectura de issues, escritura de ramas y creación de PR. Poder leer un repositorio público o figurar como administrador no demuestra los permisos efectivos de la integración. Cuando una operación carezca de evidencia, se muestra como pendiente de comprobar. Un 403 en publicación conserva el SHA y la evidencia. Una vez corregido el acceso, se reanuda desde `publishing` si la base y el candidato siguen vigentes.
- **Comprobaciones reproducibles.** Los comandos de preparación y checks se declaran como listas de argumentos en un perfil de confianza y se ejecutan dentro del entorno de tarea. Se valida el perfil sobre la base al dar de alta el proyecto. Si la base falla o el perfil carece de checks aplicables, el operador recibe la causa antes de consumir una ronda de agentes. El informe identifica el SHA, el comando, su código de salida y su duración.
- **Separación de accesos.** Cada rol recibe solo el clon y los artefactos que necesita. Las herramientas de un agente y los procesos que ejecutan código del proyecto quedan fuera de la base de estado, la credencial de GitHub, los proyectos vecinos y el socket Docker del host. La autenticación del proveedor se limita a su ejecución. Los checks se ejecutan sin las credenciales de agentes.
- **Aislamiento de servicios.** Con Compose se persisten el identificador de proyecto, el perfil aprobado y el SHA de cada verificación. Base, candidato y reintentos tienen redes, volúmenes y copias propios. El servicio recupera y retira exclusivamente los recursos que creó, incluso después de una cancelación o un reinicio. La prueba del proyecto nunca puede seleccionar por sí misma el contexto Docker del controlador.
- **Evidencia local.** Por trabajo se conserva su repositorio, issue, plan, versión de la política, base y candidato, intentos, decisiones y referencia al PR. Por etapa se registran versión de CLI, proveedor, modelo si se conoce, duración, reintentos y causa de fallo. El uso comunicado por el proveedor conserva unidad y procedencia. Los valores ausentes son desconocidos. Una estimación de coste se guarda aparte de un cargo efectivamente facturado. Los logs se filtran antes de guardarse o resumirse en GitHub.

## Referencia de diseño

La fábrica v2 de ZazenCodes documenta un flujo de issues, agentes por rol, checks y PR. MASF adopta esas ideas con orquestación central, una implementación propia y proveedores sustituibles. Esta especificación se ha redactado para MASF. El proyecto de referencia declara GPLv3. Si se decide incorporar material de allí, se debe identificar el archivo, la revisión y su licencia antes de integrarlo. [Diseño original, revisión fijada](https://github.com/zazencodes/zazencodes-season-3/blob/de4598a8fab54425366952d886bbdb083f075af6/src/software-factory-claude-code-v2/DESIGN.md) · [Licencia del original](https://github.com/zazencodes/zazencodes-season-3/blob/de4598a8fab54425366952d886bbdb083f075af6/src/software-factory-claude-code-v2/LICENSE).
