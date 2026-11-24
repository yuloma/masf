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

**Aceptación.** Cada adaptador supera el mismo conjunto de casos de salida válida, salida inválida, tiempo agotado y fallo del proceso. Las muestras de las CLIs llevan versión y carecen de credenciales. Una ejecución mezcla al menos dos proveedores y el revisor recibe el diff y los checks aunque el implementador pertenezca a otro proveedor. Se registra por separado qué combinaciones se ejercitaron con cuentas reales y cuáles con dobles de prueba. Si la CLI devuelve un error de acceso o cuota, se conserva el trabajo y su causa para que pueda reanudarse.

## I4 · Instalar el servicio y ejecutar el piloto completo

**Resultado para el operador.** `masf --config FILE serve` sondea periódicamente y procesa la cola con los límites configurados. El servicio espacia las consultas ante límites de GitHub y recupera tareas tras reinicios. Se entregan instrucciones de instalación y actualización en Linux, un ejemplo opcional de servicio de usuario de systemd y la consulta de resultados mediante `status`.

**Aceptación.** Aplicar el protocolo del piloto desde un checkout limpio. La segunda consulta y el reinicio durante la publicación mantienen una única rama y un PR por issue. Un segundo repositorio funciona con la misma instalación y configuración adicional. El informe identifica la versión instalada, el destino, los agentes realmente ejecutados, los checks y las intervenciones humanas.

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

La aceptación de I2 incluye autorizar con `resume --allow-protected-path RUTA` un archivo exacto de un trabajo bloqueado. La regresión debe conservar el candidato y las rondas, rechazar comodines y rutas no canónicas, mantener la comprobación de otros archivos e invalidar el permiso si cambia la huella de la issue.

La aceptación de recuperación de I2 incluye una construcción interrumpida tras escribir archivos. La reanudación debe reutilizar la copia registrada del mismo SHA y alcance, conservar el plan, contar el siguiente intento y pasar después por checks y revisión. `resume --reuse-workspace RUTA` permite recuperar una copia histórica del propio trabajo y rechaza rutas de otro trabajo o del almacén Git.

La aceptación de diagnóstico de I3 incluye salida en streaming de Cursor con resultado terminal, rechazo de flujos incompletos y conservación de actividad anterior a un timeout. Un proceso que produce abundante stderr debe terminar y entregar su actividad sin bloquear la captura.

La aceptación de Cursor comprueba además una revisión no interactiva capaz de utilizar Shell para lectura y verificación, con el código montado para lectura. El diff del candidato se entrega con sus saltos de línea y el contenido del transporte estructurado permanece fuera de los mensajes persistidos de error.
