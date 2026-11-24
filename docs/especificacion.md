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

## Proveedores y contrato de etapa

| Id en MASF | Interfaz no interactiva de referencia | Tratamiento del resultado |
| --- | --- | --- |
| `codex` | `codex exec --json` | Leer los eventos JSONL y el resultado final. El adaptador puede solicitar un esquema de salida. |
| `cursor` | `agent -p --output-format json` | Comprobar la salida del proceso y extraer el contenido del campo `result` del JSON de Cursor. |
| `claude` | `claude -p --output-format json` | Comprobar el resultado de Claude Code. Si se solicita `--json-schema`, validar la presencia y la forma de `structured_output`. |

Cada trabajo puede usar uno, dos o tres proveedores sin mantener un IDE abierto. Se comprobará la autenticación con la instalación y el plan del operador. Las interfaces están documentadas por los proveedores a 23 de noviembre de 2026. [OpenAI](https://learn.chatgpt.com/docs/non-interactive-mode) · [Cursor](https://cursor.com/docs/cli/headless) · [Formato de Cursor](https://cursor.com/docs/cli/reference/output-format) · [Anthropic](https://code.claude.com/docs/en/headless).

El servicio entrega a cada adaptador el identificador de trabajo e intento, rol, revisión de código, directorio asignado, plazo, permisos y artefactos de entrada. Espera un resultado estructurado y versionado con estado, resumen y los datos propios del rol. Planificación devuelve criterios identificables o una pregunta. Implementación devuelve un resumen y relaciona cada criterio con los cambios. Revisión devuelve un veredicto y sus hallazgos. Los adaptadores convierten el formato del proveedor y validan los campos exigidos antes de avanzar.

Cada etapa recibe sus entradas por contenido o archivos. Un id de sesión del proveedor puede facilitar el diagnóstico, pero otra CLI debe poder continuar solo con el plan, el diff y las evidencias. Una respuesta truncada, un proceso interrumpido o un error de cuota se conservan como fallo. Cambiar de proveedor o ampliar el número de rondas requiere una decisión explícita del operador.

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
