# Plan de implementación

Cada incremento deja algo que el operador puede comprobar y que el siguiente agente puede usar. Se empieza por descubrir trabajo real con acceso de lectura. La primera ejecución de pago llega cuando se puede entregar y revisar un PR completo.

Para el encargo de instalación completa, usa [INSTALL.md](../INSTALL.md). I1 a I4 son hitos de ese mismo encargo. Reutiliza lo que ya esté implementado y continúa desde cada hito hasta que el servicio procese la issue piloto y publique su PR.

## I1 · Registrar repositorios y descubrir trabajo

**Resultado para el operador.** Se pueden registrar y consultar issues desde un paquete nativo o desde la imagen del controlador. En Docker el host aporta Engine y Compose. En la instalación nativa de I1 bastan Python 3.11 o superior, SQLite y, para consultar GitHub, `gh` autenticado con lectura de los repositorios elegidos.

**Entrega.** Añadir `pyproject.toml` con el ejecutable `masf`, carga y validación del [TOML](../examples/factory.toml), un adaptador de lectura de GitHub y la primera migración de SQLite. La configuración por defecto vive en `$XDG_CONFIG_HOME/masf/config.toml` y el estado en `$XDG_STATE_HOME/masf/`. Cuando esas variables están vacías se utilizan `~/.config` y `~/.local/state` respectivamente. El parámetro global `--config` permite seleccionar otro archivo.

Entregar un Dockerfile que instale ese mismo paquete, Git y `gh`, y documentar cómo generar `masf-controller:pilot`. Usar el [Compose central](../examples/docker/compose.yaml) para `doctor`, `scan` y `status`. La configuración y el estado son externos al contenedor. `doctor --offline` comprueba los dos formatos del registro, la lista de archivos del perfil y el servicio solicitado mediante validación local. Los errores incluyen el repositorio y el campo. La comprobación del modelo Compose combinado y su servicio efectivo corresponde a I2.

| Comando previsto | Efecto verificable |
| --- | --- |
| `masf --config FILE doctor --offline` | Valida estructura, rutas de roles, repositorios y límites sin acceder a la red. Distingue un proveedor deshabilitado de un error de configuración. |
| `masf --config FILE scan --dry-run` | Consulta repositorios habilitados y lista las issues elegibles con su motivo. El estado local y GitHub quedan intactos. |
| `masf --config FILE scan` | Inserta cada issue elegible una vez y guarda su identidad e instantánea inicial. Repetirlo informa de lo que ya está registrado. |
| `masf --config FILE status` | Muestra repositorio, número de issue y estado. Un registro recién creado figura como `queued`. |

**Aceptación.** En una instalación limpia del paquete, la configuración de ejemplo y un registro histórico sin Compose pasan `doctor --offline`. Si falta una sección obligatoria o hay un tipo incorrecto, el comando devuelve código 1 y muestra en stderr la ruta del campo. Para un repositorio usa su slug o el índice desde cero si no hay un slug válido. Los tres roles se comprueban tras combinar las asignaciones globales y locales. En un manifiesto Compose con contenido, el documento es una tabla. Si declara `services`, debe ser una tabla de nombres a definiciones de servicio, que también son tablas.

Con un GitHub simulado que tenga dos páginas, una issue sin etiqueta, un PR etiquetado y dos repositorios con el mismo número de issue, solo se admiten las issues elegibles de los repositorios habilitados. Dos procesos que ejecuten `scan` sobre la misma base dejan una sola fila por issue. Un error de acceso se distingue de una cola vacía y permite que los otros repositorios se procesen. La suite se puede ejecutar sin cuentas de IA, Docker ni credenciales de escritura.

La suite nativa ejecuta dos procesos de la CLI con el mismo TOML y la misma SQLite. El `gh` simulado espera a ambos al resolver el primer commit base, antes de insertar trabajos. Los dos procesos salen con código 0 y dejan cuatro filas `queued`, una por cada combinación de ID de repositorio y número de issue elegible. Un HTTP 403 en el primer repositorio aparece en stderr con su nombre y produce código 1, mientras las issues elegibles del segundo repositorio quedan en cola. `scan --dry-run` informa de las issues elegibles mediante consultas de lectura y conserva el estado local tanto si la cola existe como si todavía no se ha creado.

En Python 3.11.2, `python3 -m unittest discover -v` terminó con código 0 en un entorno sin credenciales ni servicios externos. Descubrió 39 pruebas, pasaron 32 y se omitieron las 7 optativas que requieren Docker Engine.

Como prueba de empaquetado en un host con Docker, construir la imagen, ejecutar `doctor --offline` sin montar el socket y comprobar que un `scan` y un `status` en contenedores distintos conservan la misma issue en SQLite. Ejercitar en la suite nativa una entrada histórica sin bloque Compose, la entrada multiservicio de ejemplo y una ruta que intenta salir del directorio de configuración.

## I2 · Entregar el primer PR con Codex

**Resultado para el operador.** `masf --config FILE run --once` descubre trabajo y ejecuta o reanuda como máximo una issue. Al principio se asignan los tres roles a Codex. Se incorporan el runner aislado, la copia de trabajo, los checks sobre la base y el candidato, el registro por etapa y el publicador de GitHub. `masf --config FILE resume OWNER/REPO#N` devuelve a la cola un trabajo bloqueado cuya causa se ha atendido. La instalación documenta cómo construir localmente la imagen del runner y autenticar el proveedor elegido. `doctor` sin `--offline` comprueba el acceso de lectura a GitHub y las dependencias del runner e informa de aquello que solo puede verificarse con una ejecución real.

**Aceptación.** Un proveedor de prueba y un remoto Git temporal reproducen el camino desde `queued` hasta un único PR simulado. Cubrir un check fallido seguido de corrección, una revisión que pide cambios, una ruta protegida y una caída entre crear el PR y persistir su número. Solo se publica el SHA verificado. En el repositorio desechable del [piloto](piloto.md) se comprueba el recorrido con Codex real, y se registra la URL y el SHA. Esa prueba de campo requiere el acceso del operador y se informa por separado de la suite.

Simular también lectura correcta de la issue y un 403 al crear la rama o el PR. `doctor` debe distinguir los permisos observados de los desconocidos. Corregido el permiso, `resume` debe poder publicar el mismo SHA sin otra llamada de IA si siguen vigentes los checks y la revisión.

La aceptación de evidencia verifica la huella del motor y la política al registrar por descubrimiento o vinculación, la configuración filtrada y el vínculo entre checks, revisión, intención y PR confirmado. Reanudar un trabajo histórico con plan, candidato y checks vigentes conserva esos resultados y muestra los datos de procedencia ausentes como desconocidos. [Formato de etapa v1](especificacion.md#formato-de-evidencia-de-etapa-v1).

**Ejecución en Docker.** Incorporar al controlador el cliente Docker y Compose y conectar el socket solo mediante la [extensión del motor](../examples/docker/engine.yaml). Implementar los modos de imagen individual y `repositories.compose` del [contrato](docker.md), incluidos el límite de tiempo, el código de salida de cada comando, la copia limpia por SHA, la persistencia del nombre de proyecto y la recuperación de recursos.

**Aceptación con Engine local.** Ejecutar primero la [prueba independiente de PostgreSQL](../examples/docker/smoke-profile.sh). Repetir a través de MASF con el perfil de ejemplo y sin herramientas del proyecto en el host. Verificar la misma tarea con controlador nativo y contenedorizado en instalaciones de prueba independientes. Cubrir dos verificaciones simultáneas, servicios no saludables, un check con salida distinta de cero, cancelación y recuperación después de crear un volumen. Comprobar que un manifiesto o `.env` introducido en la rama no cambia el despliegue, que un perfil con un montaje del socket se rechaza antes de `up` y que el stack de desarrollo sobrevive a la limpieza. Registrar por separado la prueba con Engine de las pruebas con dobles.

La [suite optativa](../tests/test_engine.py) usa perfiles privados derivados del ejemplo. Prueba el montaje y la construcción desde copias de la base, del candidato y del mismo SHA en un reintento. Compara el modelo efectivo y el estado de los servicios con el perfil aprobado. Tras cada verificación comprueba que el stack de control conserva los contenedores, los recursos y la fila de prueba. Emite evidencia JSON con el SHA, la huella del modelo, el proyecto y los inventarios antes y después de la limpieza, incluida la recuperación.

Desde la raíz del checkout, prepara Docker Engine local con Compose y las imágenes locales `postgres:17-alpine`, `masf-runner:pilot` y `masf-controller:pilot`. El [contrato de Docker](docker.md) describe la preparación de las imágenes. Ejecuta estos bloques en orden. Cada modalidad crea y elimina su propia instalación temporal. La modalidad nativa requiere Python 3.11 o superior con PyYAML.

```bash
python3 -m unittest discover -v
bash examples/docker/smoke-profile.sh
```

```bash
(
  set -eu
  i2_root=$(realpath "$(mktemp -d)")
  trap 'rm -rf -- "$i2_root"' EXIT
  export MASF_TEST_ENGINE_CONFIG_DIR="$i2_root/config"
  export MASF_TEST_ENGINE_STATE_DIR="$i2_root/state"
  export MASF_JOBS_DIR="$i2_root/jobs"
  mkdir -m 700 "$MASF_TEST_ENGINE_CONFIG_DIR" "$MASF_TEST_ENGINE_STATE_DIR" "$MASF_JOBS_DIR"
  export DOCKER_HOST="unix://${MASF_DOCKER_SOCKET:-/var/run/docker.sock}"
  MASF_TEST_ENGINE=1 python3 -m unittest tests.test_engine -v
)
```

```bash
(
  set -eu
  i2_root=$(realpath "$(mktemp -d)")
  trap 'rm -rf -- "$i2_root"' EXIT
  export MASF_CONFIG_DIR="$i2_root/config" MASF_STATE_DIR="$i2_root/state"
  export MASF_JOBS_DIR="$i2_root/jobs"
  mkdir -m 700 "$MASF_CONFIG_DIR" "$MASF_STATE_DIR" "$MASF_JOBS_DIR"
  export MASF_UID="$(id -u)" MASF_GID="$(id -g)"
  export MASF_DOCKER_SOCKET="${MASF_DOCKER_SOCKET:-/var/run/docker.sock}"
  export MASF_DOCKER_GID="$(stat -c %g "$MASF_DOCKER_SOCKET")"
  export DOCKER_HOST="unix://$MASF_DOCKER_SOCKET"
  unset GH_TOKEN MASF_CODEX_AUTH MASF_CURSOR_AUTH MASF_CLAUDE_AUTH
  i2_project=$(basename "$i2_root" | tr '[:upper:]' '[:lower:]' | tr . -)
  i2_compose=(docker compose --env-file /dev/null -p "masf-i2-$i2_project"
    -f examples/docker/compose.yaml -f examples/docker/engine.yaml)
  cleanup_i2() {
    local result=$?
    "${i2_compose[@]}" down --remove-orphans || result=1
    rm -rf -- "$i2_root" || result=1
    exit "$result"
  }
  trap cleanup_i2 EXIT
  "${i2_compose[@]}" run --rm --no-deps \
    --entrypoint /opt/venv/bin/python -w /opt/masf-test-source \
    -v "$(pwd -P):/opt/masf-test-source:ro" \
    -v "$MASF_CONFIG_DIR:/tmp/masf-test-config" \
    -e MASF_TEST_ENGINE=1 -e MASF_TEST_ENGINE_CONFIG_DIR=/tmp/masf-test-config \
    -e MASF_TEST_ENGINE_STATE_DIR=/var/lib/masf \
    controller -m unittest tests.test_engine -v
)
```

El manifiesto central conserva la misma ruta absoluta de trabajos dentro y fuera del controlador contenedorizado. El montaje adicional de configuración permite a la suite generar los perfiles de prueba. Las verificaciones usan el Engine local y los repositorios Git temporales.

| Comprobación | Resultado observado |
| --- | --- |
| `python3 -m unittest discover -v` | 49 pruebas, 40 correctas y 9 omitidas por requerir Engine. Incluye la comprobación con un doble del comando guardado para limpiar la imagen. |
| `bash -n examples/docker/smoke-profile.sh` y `bash examples/docker/smoke-profile.sh --help` | Ambas finalizaron con código 0. |
| `bash examples/docker/smoke-profile.sh` | Código 127 al invocar Docker. La prueba de PostgreSQL queda pendiente. |
| `docker compose version` y comprobación de `/var/run/docker.sock` | El cliente y el socket local no estaban disponibles. Las ejecuciones con Engine desde ambos controladores quedan pendientes. |

## I3 · Intercambiar los tres proveedores

**Resultado para el operador.** Añadir Cursor CLI y Claude Code como adaptadores del mismo contrato. `plan`, `build` y `review` se asignan desde la configuración, con excepciones por repositorio. Un proveedor deshabilitado o sin roles asignados puede estar ausente de la instalación. La preparación del runner y las instrucciones de autenticación cubren los tres proveedores de forma independiente.

**Aceptación.** La matriz de `plan`, `build` y `review` de cada adaptador cubre resultado válido, JSON inválido, campos obligatorios ausentes o de tipo incorrecto, salida truncada, proceso fallido y tiempo agotado. Incluye planes con pregunta, revisiones que solicitan cambios y las envolturas admitidas. Un flujo con proveedores de prueba distintos entrega al revisor el plan persistido, el diff desde la base y los checks del SHA candidato. Los fallos de acceso y cuota conservan el trabajo, el proveedor, la causa depurada y la etapa reanudable, además de la copia de construcción o el candidato y sus checks según corresponda. [Catálogo de muestras y procedencia](../tests/fixtures/README.md).

La aceptación de intervención de I3 responde una pregunta del plan y verifica que el siguiente intento recibe el texto, la respuesta y la referencia del intento cuando existe. Reasigna un rol a un proveedor habilitado y amplía las rondas agotadas, conservando el consumo y el historial del alcance.

La aceptación de trazabilidad registra un intento activo, una etapa correcta y fallos antes y después de obtener la versión de CLI. Verifica el cambio de proveedor y modelo solicitado entre intentos, la identidad efectiva comunicada o desconocida y las mediciones con unidad, procedencia y valor cero. Comprueba que las explicaciones corresponden al resultado y que los intentos anteriores conservan su configuración.

Ejecuta `python3 -m unittest discover -v` con Python 3.11 o superior y las dependencias de MASF instaladas. La matriz y el flujo con Git temporal simulan los proveedores y GitHub y se ejecutan sin autenticación. La captura con cuentas reales es una comprobación optativa del operador y la validación entre cuentas corresponde al [piloto](piloto.md#capturas-optativas-de-proveedores).

## I4 · Instalar el servicio y ejecutar el piloto completo

**Resultado para el operador.** `masf --config FILE serve` sondea periódicamente y procesa la cola con los límites configurados. El servicio espacia las consultas ante límites de GitHub y recupera tareas tras reinicios. Se entregan instrucciones de instalación y actualización en Linux, un ejemplo opcional de servicio de usuario de systemd y la consulta de resultados mediante `status`.

**Aceptación.** Aplicar el protocolo del piloto desde un checkout limpio. La segunda consulta y el reinicio durante la publicación mantienen una única rama y un PR por issue. Un segundo repositorio funciona con la misma instalación y configuración adicional. El informe identifica la versión instalada, el destino, los agentes realmente ejecutados, los checks y las intervenciones humanas.

La aceptación de intervención de I4 aplica decisiones concurrentes desde la CLI y la API sobre la misma revisión. Simula pérdida del proceso antes y después de confirmar SQLite, reenvía la misma solicitud y verifica un recibo, una transición y un despacho del mismo trabajo. Agota y amplía el presupuesto de recuperación con el servicio en marcha y comprueba que los intentos anteriores persisten al reiniciar.

La aceptación del sondeo de PR se ejecuta localmente con `python3 -m unittest tests.test_observation -v`.

1. Admitir issues y enumerar las páginas de PR de los repositorios habilitados. Identificar los PR sin trabajo en `status --pr`, comprobar la procedencia de la marca antes de vincular automáticamente y permitir una asociación local mediante `link-pr`.
2. Conservar la identidad, referencias, borrador y estado del PR. Contrastar con el detalle los PR que desaparezcan de un listado completo antes de marcar cierre o merge. Incluir un trabajo cuyo único vínculo registrado sea la URL del PR.
3. Guardar los check runs, commit statuses, workflows, ejecuciones, jobs e intentos por head. Distinguir requisitos leídos, resultados esperados ausentes y requisitos cuya lectura o aplicación no se pudo verificar.
4. Registrar estados y conclusiones separados, incluidas peticiones de intervención. Recuperar anotaciones y extractos filtrados de fallo y permitir reintentar los diagnósticos inaccesibles. Comprobar la correlación de checks comunicados después del job y el filtrado de credenciales con escapes de terminal y valores marcados por Actions.
5. Mantener el sondeo con un worker ocupado y conservar candidato y rondas. Simular errores de consulta y cuota, conservar la última información válida y seguir con otros PR mientras GitHub permita consultar.
6. Repetir el sondeo y abrir de nuevo la misma SQLite. Consultar mediante `status` y `logs --pr OWNER/REPO#PR --sha SHA` sin duplicar transiciones. Verificar un head nuevo, un rerun que reutilice el ID de un check y una ejecución nueva del mismo workflow. Simular también un fallo temporal al recuperar un intento histórico y comprobar que su estado previo sigue disponible hasta que se restablezca la consulta. [Contrato de observación v1](especificacion.md#formato-de-observación-v1).
7. Reproducir el PR #23 abierto y vinculado al trabajo #10. La evidencia simulada atribuye el CI al head comunicado por el operador `d8cda48a51ee46eb285ac866e8b58d4ad89415db` y conserva `bb747e16b42c2aaf05ac9275c9625cde90a49aec` como candidato. La simulación representa el merge de `main` en la rama del PR mediante los padres del commit. La comprobación con evidencia real del CI de ese PR queda pendiente.

8. Simular un fallo funcional de CI, su clasificación, el intento de reparación y el CI posterior del mismo PR. Seguir los dos SHA desde el historial, repetir un sondeo con datos tardíos y verificar que los secretos ficticios quedan filtrados.

Publicar en el README los comandos para arrancar y actualizar el controlador mediante Compose, consultar su estado y recrearlo conservando SQLite y trabajos. Usar la vía contenedorizada en el piloto de servicio continuo y comprobar `status` tras la recreación. Mantener la vía nativa para operadores que ya gestionan procesos Linux.

## Encargo para iniciar I1

Este prompt corresponde a una petición expresa de implementar solo I1. Para la instalación completa, sigue [INSTALL.md](../INSTALL.md).

> Trabaja en `yuloma/masf`. Lee `AGENTS.md`, `docs/especificacion.md`, `docs/docker.md`, `examples/factory.toml` y el apartado I1 de `docs/implementacion.md`. Implementa I1 en una rama del repositorio. Entrega el paquete instalable, los cuatro comandos, la imagen del controlador y persistencia idempotente. Usa ambos registros de ejemplo y ejecuta la suite sin servicios externos en un entorno virtual limpio. En un host con Docker, construye la imagen y verifica la aceptación de empaquetado con el Compose central. Actualiza el README y deja un PR con resultados y limitaciones observadas. La aceptación requiere que una issue elegible se pueda registrar como `queued` y consultar tras recrear el contenedor.

Para preparar I2, el siguiente agente debe poder partir del paquete y la cola entregados en I1. Las decisiones nuevas que cambien el contrato público se reflejan en la especificación, el ejemplo y la aceptación del incremento correspondiente.

## Verificación de esta implementación

La suite de Python usa un remoto Git temporal y dobles de los proveedores y de GitHub. Comprueba la paginación, las claves por repositorio, el vencimiento de reservas, el rechazo de rutas protegidas y la recuperación del mismo candidato tras un 403 o una respuesta perdida del publicador. Las pruebas optativas con Engine ejecutan dos proyectos Compose independientes, comprueban el código de un comando fallido y recuperan volúmenes tras una limpieza interrumpida. Los valores opcionales `MASF_SMOKE_BASE_SUBNET` y `MASF_SMOKE_CANDIDATE_SUBNET` deben preservar el dato de control y permitir retirar ambos proyectos al finalizar. [Comandos de aceptación](docker.md#autenticación-y-diagnóstico).

## Regresiones de operación autónoma

Antes de entregar una actualización, los checks de `main` deben pasar dentro del runner instalado. Las aceptaciones incluyen una respuesta de Codex sin `turn.completed`, un timeout de infraestructura que preserve SHA y rondas, recuperación de un PR creado antes de avanzar la base, interrupción y limpieza de un check activo, plazo acumulado de checks y respeto de los tiempos de espera de GitHub. La CI ejecuta las pruebas con Engine mediante la imagen de runtime.

La regresión de Cursor usa una muestra de la CLI `2026.11.11-b912975`, sin credenciales. Su envoltura JSON puede concatenar progreso y respuesta en `result`. El adaptador debe aceptar el objeto final completo y rechazar objetos truncados o seguidos de texto adicional.

La entrada de procesos usa un archivo temporal privado para conservar prompts grandes mientras se consulta el plazo o la elegibilidad. Una regresión con un lector lento verifica el contenido completo en Python 3.11 y versiones posteriores.

### Recuperación tras terminar el controlador

Desde la raíz del repositorio, con las dependencias de MASF instaladas, ejecuta:

```bash
python3 -m unittest tests.test_process_recovery.ProcessRecovery -v
```

Los cinco casos arrancan la CLI con `run --once`, un remoto Git temporal, un servidor HTTP de GitHub en loopback y un proveedor de prueba. Ejecutan un check de prueba en la copia local de cada SHA y funcionan sin Docker, credenciales ni cuentas de IA. El proceso recibe `SIGKILL` tras confirmar la reserva, el push o la creación del PR en el simulador. El harness verifica que la reserva vigente impide otro intento y adelanta su vencimiento en SQLite para que la CLI la reclame sin esperar 90 segundos.

Los casos de publicación repiten el escenario con la base estable y con un avance de `main`. Si solo se subió la rama y la base avanzó, el reemplazo verifica la nueva base y el nuevo candidato y vuelve a revisar el candidato antes de crear el PR. Si ya se creó el PR, recupera su número y su SHA originales. Los registros de roles y checks comprueban la reutilización de la evidencia vigente. Las regresiones con excepciones simuladas siguen formando parte de la suite.

Para interrumpir un check real dentro de Compose, usa un Docker Engine local con el plugin Compose y la imagen `masf-runner:pilot` ya disponible. La CI ejecuta el caso en el paso de pruebas con Docker tras construir esa imagen:

```bash
MASF_TEST_ENGINE=1 python3 -m unittest tests.test_process_recovery.ComposeProcessRecovery -v
```

Si el Engine ha agotado sus pools automáticos, el harness acepta las mismas variables de subred que el smoke. El proyecto de control usa `MASF_SMOKE_BASE_SUBNET` y los checks usan `MASF_SMOKE_CANDIDATE_SUBNET`; la recuperación debe retirar el check anterior antes de reutilizar su subred. Ambas subredes deben estar libres.

```bash
MASF_SMOKE_BASE_SUBNET=10.253.248.0/28 MASF_SMOKE_CANDIDATE_SUBNET=10.253.248.16/28 MASF_TEST_ENGINE=1 python3 -m unittest tests.test_process_recovery.ComposeProcessRecovery -v
```

El caso Compose espera una señal escrita por el check dentro del contenedor antes de matar el controlador. Después cambia el volumen declarado en el perfil del operador. El reemplazo retira el proyecto anterior con el manifiesto congelado, completa el check en un proyecto nuevo y retira sus recursos y su journal. La espera del reemplazo exige la señal de un checkout asociado a un journal distinto. La señal anterior permanece en su checkout tras la caída. Un proyecto Compose de control conserva su contenedor, su red y el dato guardado en su volumen durante y después de la recuperación.

El manifiesto de ejecución se conserva en un directorio privado `masf-check-<id>-profiles` junto al checkout, con permisos 0700 y referenciado desde el journal de limpieza. La aceptación comprueba que sobrevive al SIGKILL y que se retira al recuperar el proyecto anterior; el perfil del intento nuevo también debe desaparecer tras completar sus checks.

**Regresión de trazabilidad del 25 de noviembre de 2026.** Tras adaptar la prueba a la ubicación privada del perfil, `ComposeProcessRecovery.test_check_interrupted` pasó con Engine local y subredes explícitas en 45,41 segundos. `python3 -m unittest discover -v` descubrió 128 pruebas y terminó correctamente con 10 omisiones optativas de Engine; `doctor --offline` también pasó.

**Resultado local del 25 de noviembre de 2026 con Python 3.14.4.** Tras integrar la base vigente, `python3 -m unittest discover -v` descubrió 115 pruebas. Pasaron 105, incluidos los cinco casos de recuperación con `SIGKILL` sin Engine, y se omitieron 10 por requerir Docker Engine. El proveedor simulado acepta el callback de actividad del motor actual, el servidor simula también la observación de PR y CI, y la sustitución de la reserva se contrasta con los tokens conservados en los eventos.

**Resultado con Engine del 25 de noviembre de 2026.** `ComposeProcessRecovery.test_check_interrupted` pasó con Docker Engine local en 45,35 segundos y las dos subredes explícitas del ejemplo. Se verificaron el SIGKILL del controlador durante el check, la retirada mediante el manifiesto congelado, una sola ejecución de reemplazo, la conservación del plan y la construcción, un único PR con checks y revisión del mismo SHA, la limpieza final y la identidad y el dato del proyecto de control. La espera del reemplazo excluye el journal y la señal que dejó el proceso terminado. Estos ensayos utilizan proveedores y GitHub simulados.

La aceptación de I2 incluye autorizar con `resume --allow-protected-path RUTA` un archivo exacto de un trabajo bloqueado. La regresión debe conservar el candidato y las rondas, rechazar comodines y rutas no canónicas, mantener la comprobación de otros archivos e invalidar el permiso si cambia la huella de la issue.

La aceptación de recuperación de I2 incluye una construcción interrumpida tras escribir archivos. Reanudar con `--timeout-seconds N --workspace-reference REFERENCIA` conserva el plan, los archivos y la ronda contabilizada, registra un nuevo intento y pasa después por checks y revisión. Prueba copias actuales e históricas del mismo SHA y alcance, y el rechazo de copias ajenas o sin evidencia registrada.

La aceptación de diagnóstico de I3 incluye salida en streaming de Cursor con resultado terminal, rechazo de flujos incompletos y conservación de actividad anterior a un timeout. Un proceso que produce abundante stderr debe terminar y entregar su actividad sin bloquear la captura.

La aceptación de recuperación autónoma de I2–I4 incluye provocar un timeout tras escribir archivos, persistir `retry_wait`, reiniciar el controlador y continuar sin intervención sobre la misma copia. Se conserva el SHA y los checks al repetir una revisión incompleta. Un conflicto real de Git debe volver al implementador y producir un candidato con ambos padres, checks y revisión nuevos y un solo PR. La espera debe respetar el reloj persistido y los límites de GitHub, sin impedir atender otra issue. Dos fallos adicionales consumen el presupuesto predeterminado y el siguiente deja un bloqueo diagnosticable. Acceso denegado y rutas protegidas siguen necesitando autorización. La configuración de ejemplo documenta `max_recovery_attempts` y `recovery_backoff_seconds`. Con un PR ya abierto, verificar que un timeout de reparación reutiliza la copia y la misma ronda, que los reinicios agotan el presupuesto persistido y que un conflicto de base conserva ambos ancestros, repite checks y revisión y actualiza el mismo PR.

La aceptación de Cursor comprueba además una revisión no interactiva capaz de utilizar Shell para lectura y verificación, con el código montado para lectura. El diff del candidato se entrega con sus saltos de línea y el contenido del transporte estructurado permanece fuera de los mensajes persistidos de error.

La aceptación de I2 incluye un conflicto real al avanzar la base y su resolución mediante `resume --resolved-commit SHA`. Se verifica que el commit integra candidato y base, que se rechazan referencias o commits ajenos, que se conserva el número de rondas y que se repiten los checks y la revisión sobre el SHA resuelto.

### Recuperación de conflictos tras abrir el PR

Con un remoto Git temporal, GitHub simulado y un proveedor de prueba, abrir un PR y avanzar la base. Confirmar que el sondeo reclama un conflicto de contenido o de modificación/eliminación, que el implementador recibe el árbol combinado y los criterios y que el mismo PR recibe un nuevo head con ambos ancestros, checks y revisión nuevos y su propio CI. Comprobar que una base compatible, un CI pendiente, la protección de rama, la mergeabilidad desconocida y los errores temporales del sondeo dejan intacto el presupuesto de resolución.

Simular que GitHub sigue devolviendo `dirty` para una base que Git integra limpiamente. Comprobar que el PR permanece en `pr_open`, que se vuelve a consultar la mergeabilidad y que el CI del mismo SHA pasa de pendiente a correcto durante el desacuerdo. Confirmar que se conserva el diagnóstico, la mergeabilidad queda pendiente y los sondeos repetidos dejan intacto el presupuesto y la cola de resolución.

Durante la recuperación, provocar otro avance de base, un cambio ajeno de head, cierre, merge, retirada de etiqueta y edición de alcance. Verificar la espera persistente y la revalidación antes de actualizar. Provocar dos workers concurrentes, una interrupción del implementador, un reinicio después del push y una respuesta perdida. Comprobar que el mismo PR y los cambios de terceros se conservan. Con presupuesto cero o agotado, marcadores, ascendencia incompleta, ruta protegida o push denegado, comprobar el diagnóstico y las intervenciones por alcance, ruta, head, permisos o commit resuelto. Los intentos, las decisiones y los resultados quedan disponibles por SHA en `status --json`, `history` y la API v1.

Los casos `test_open_pr_*` de `tests.test_factory.Delivery` ejecutan estas comprobaciones locales con Git real y dobles de GitHub y proveedor. Los resultados del piloto con servicios reales se registran por separado en [el piloto](piloto.md).

En el entorno local, `python3 -m unittest discover -v` ejecutó 155 casos. Pasaron 141 y se omitieron 14 que requieren Docker Engine o Playwright y Chromium. También pasaron `doctor --offline` y la lectura de los TOML con Python.

### Seguimiento y reparación de CI del mismo PR

**Aceptación local.** Con la configuración de [ejemplo](../examples/factory.toml), verificar que `status` muestra etapa, CI, SHA, consumo e intervención. `status --json` e `history OWNER/REPO#N` deben mostrar la evidencia por SHA y las decisiones por intento. Un remoto Git temporal, GitHub simulado y un proveedor de prueba deben reproducir PR abierto, fallo confirmado, construcción, checks y revisión del mismo candidato, push condicionado al head observado y CI correcto del mismo PR.

El CI pendiente y el rerun en curso dejan la rama intacta. Un sondeo duplicado conserva el mismo diagnóstico y una sola ronda. Probar fallos técnicos con espera y límite, diagnóstico insuficiente, rondas agotadas y las operaciones `resume --accept-head`, `resume --add-rounds`, `resume --allow-protected-path` y `link-pr`. Comprobar que el cambio de alcance, cierre o head ajeno impide escribir en el remoto. Simular la interrupción durante construcción y después del push y comprobar que el historial conserva los SHA de ambos ciclos.

Comprobar también el ciclo combinado de observación y reparación: el mismo sondeo conserva el snapshot de CI por SHA y despacha el trabajo a `building`; un trabajo en `checking` con espera técnica futura no se ejecuta ni duplica el diagnóstico. Mantener el sondeo concurrente durante los workers, las dos formas de `link-pr` documentadas y las salidas de `status --json`, `status --pr` y `logs --pr`.

La clasificación debe ignorar tiempos como `4.429` y `5.503`, números aislados y nombres de pruebas aprobadas que contengan `quota` o `rate_limit`. Un log con esos datos y `FAIL: test_check_interrupted` debe producir una reparación funcional en el mismo PR. Mantener los bloqueos de cuota y acceso para mensajes explícitos, incluidos `HTTP 429` y `status code: 403`, y los reintentos técnicos para `HTTP/1.1 503`.

**Aceptación de campo.** Ejecutar el [ensayo del PR 23](piloto.md#aceptación-de-ci-del-pr-23) con autorización del operador y conservar el PR abierto hasta que decida sobre la integración.

## I5 · API local de observación

Ejecuta `python3 -m unittest tests.test_observation_api -v`. La instalación temporal verifica por HTTP el arranque nativo en loopback con puerto predeterminado y personalizado, las lecturas concurrentes durante una escritura y la visibilidad después del commit. Compara los archivos de cola, registro, evidencia y trabajo antes y después de las solicitudes. Comprueba paginación, errores, proyecciones históricas y el rechazo de rutas manipuladas, enlaces y evidencia de otro trabajo.

La misma suite contrasta dos intentos con modelos y proveedores distintos, un fallo, checks locales, actividades y decisiones humanas del controlador. Comprueba la revisión antes y después de reanudar, el resultado de una autorización y el historial de dos SHAs de un PR con un error de lectura de GitHub simulado. Valida los montajes y los permisos declarados en el manifiesto del observador.

Con un Engine local en Linux y la imagen del controlador disponible, ejecuta el [ejemplo de despliegue](observacion.md#despliegue-en-docker) sobre una instalación temporal. Comprueba que `/v1/jobs` responde desde el host. Con `docker inspect` verifica que los dos montajes están en solo lectura, que el contenedor tiene sistema de archivos raíz de solo lectura y que los trabajos, el socket de Docker y las credenciales quedan fuera del contenedor.

## I5 · Dashboard e intervención

1. Ejecuta `python3 -m unittest tests.test_observation_api tests.test_dashboard -v`. Verifica el dashboard y las dos API de la instalación temporal. Interrumpe el controlador después del HTTP 202 y antes de aplicar. Reinicia el controlador, reenvía la misma clave y comprueba un único recibo, una única aplicación y un único despacho. Cambia el alcance entre la aceptación y la aplicación para comprobar el rechazo y que el trabajo sigue esperando una decisión. Comprueba preguntas, alcance, autorizaciones, reasignación, rondas, reanudación, autenticación, revisión obsoleta, idempotencia y copias históricas compatibles. Confirma que la vista muestra la base, el candidato y las rutas de conflicto, que solo ofrece commits de resolución conservados para el alcance y la base registrados y que el nuevo SHA repite checks y revisión. Comprueba el historial de recuperaciones, la espera automática, el límite ampliado y la continuación sin recarga manual. El CI simulado pasa por ejecución, fallo de infraestructura, intervención, fallo funcional, reparación y CI correcto. Comprueba que se conservan los resultados de ambos SHAs.
2. Con Playwright y Chromium locales, ejecuta `MASF_DASHBOARD_BROWSER=1 MASF_DASHBOARD_EVIDENCE=/tmp/masf-dashboard-evidence python3 -m unittest tests.test_dashboard_browser -v`. Contrasta las capturas con los recibos, el estado publicado en la API y las decisiones persistidas. Verifica la navegación por teclado y la vista móvil. La reanudación completa parte de un formulario del navegador y llega a un PR abierto con el Git remoto y GitHub simulados.
3. Desde la instalación central, arranca `masf --config FILE observe-api` y abre `http://127.0.0.1:8765/dashboard/`. Para intervenir, usa `--actions-token-file FILE` y mantén `masf serve` activo sobre el mismo estado. Elige una intervención en el navegador, envía la decisión y registra el ID del recibo, el trabajo y la hora. Espera a que el recibo indique la aplicación y comprueba el evento con el mismo ID y feedback filtrado. Verifica que la etapa o el intento posterior corresponde al mismo trabajo y resulta visible en el navegador. [Arranque y comprobaciones](dashboard.md).

### Revisión independiente e integración condicionada

1. Valida el registro con y sin política y prueba una autorización de ruta protegida. El valor predeterminado conserva la integración humana. Una política activa exige el rol, el método, los checks por clase y la declaración de despliegue.
2. Captura la petición de `merge_review` y el comando del proveedor simulado. Contrasta issue, criterios, base, head, diff, instrucciones accesibles y referencias de CI y checks locales. Confirma la copia de solo lectura, el directorio personal efímero y la ausencia de historial y veredictos previos. Una verificación adicional repite únicamente los checks del registro.
3. Simula rutas reservadas, efectos indirectos, hallazgos bloqueantes, checks faltantes y una respuesta inválida. Comprueba la intervención incluso con CI verde. Prueba un cambio elevado con integración acreditada y otro que requiere un ensayo de migración que el CI no ejecutó.
4. Con un remoto Git temporal, GitHub simulado y un proveedor de prueba, observa CI, revisa el PR y confirma que el adaptador condicional simulado marca listo y mezcla. Comprueba que el CI pendiente permanece en observación, el CI ausente solicita evidencia y un fallo funcional reproduce la reparación con evaluación del nuevo head.
5. Cambia base, head, alcance y política después del veredicto. Comprueba que se invalidan las decisiones correspondientes, incluidas las del operador. Simula el avance de base entre la comprobación y la mutación. El adaptador simulado rechaza la operación. El adaptador disponible de GitHub escala por carecer de una precondición sobre la base.
6. Prueba protecciones pendientes al pasar el PR a listo, permisos insuficientes, sondeos duplicados y dos workers. Interrumpe el controlador con `SIGKILL` tras persistir la intención, antes y después de la aceptación simulada. Confirma la reconciliación también con una respuesta perdida.
7. Inspecciona `status --json`, `history OWNER/REPO#N`, la API de observación y el panel. Usa `resume --retry-merge-review` para reevaluar y `resume --record-manual-merge --evidence 'MOTIVO'` para registrar una autorización manual vigente. Conserva los trabajos históricos y exige una evaluación nueva al incorporar un PR abierto.
8. Ejecuta `python3 -m unittest discover -v` y carga `examples/factory.toml` con `masf.config.load`. La ejecución real del piloto se registra por separado. La integración observada acredita un merge. La configuración indica si el repositorio lo utiliza como disparador de despliegue.

La aceptación local de las mutaciones requiere el simulador con precondición sobre ambas referencias. En GitHub queda pendiente disponer de una operación con esa garantía para activar el merge automático.
