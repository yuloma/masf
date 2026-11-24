# MASF en Docker y proyectos que usan Compose

El controlador se puede instalar en un contenedor o en un entorno virtual de Linux. En ambas instalaciones cada repositorio podrá ejecutar sus comprobaciones en una imagen individual o en un conjunto de servicios Compose. Un solo controlador podrá mantener repositorios de ambos tipos y repartir los roles entre Codex, Cursor y Claude Code.

| Pieza | Dónde se ejecuta | Qué conserva |
| --- | --- | --- |
| Controlador | Contenedor central o proceso nativo. Lee issues, lanza trabajos y publica PR. | Configuración privada, SQLite, resultados de etapas y referencias de recuperación. |
| Agente de un rol | Contenedor desechable con la CLI asignada y acceso al checkout según el rol. | El controlador recoge el plan, los cambios o la revisión antes de retirar el contenedor. |
| Comprobaciones del proyecto | Contenedor individual o servicios hermanos creados en el mismo Docker Engine local. El controlador ejecuta `setup` y `checks` en el servicio configurado. | Los logs filtrados y códigos de salida por SHA. Los volúmenes de prueba se descartan al finalizar esa ejecución. |

## Desplegar el controlador

El [manifiesto central](../examples/docker/compose.yaml) describe el despliegue. La imagen local `masf-controller:pilot` instala el ejecutable `masf`. También puedes probar el [perfil de proyecto](#probar-el-perfil-sin-instalar-masf) independientemente del controlador.

En el host del piloto se necesita Linux con Docker Engine local y el plugin Docker Compose con soporte de `up --wait`. La imagen de I1 incluye MASF, sus dependencias, Git y `gh`. Desde I2 incluye también el cliente Docker y el plugin Compose. Las CLIs de IA van en las imágenes de los agentes. El host puede ejecutar el piloto contenedorizado con Docker y las utilidades habituales de Linux, aunque carezca de Python, PHP, PostgreSQL o un editor abierto.

Desde la raíz de un checkout de MASF, prepara tres directorios privados del operador. Usa rutas absolutas, distintas y sin enlaces simbólicos. Mantén esta configuración en el host que ejecuta el daemon.

```bash
masf_root="$HOME/.local/share/masf-pilot"
export MASF_CONFIG_DIR="$masf_root/config"
export MASF_STATE_DIR="$masf_root/state"
export MASF_JOBS_DIR="$masf_root/jobs"
export MASF_UID="$(id -u)" MASF_GID="$(id -g)"
install -d -m 700 "$masf_root" "$MASF_CONFIG_DIR" "$MASF_CONFIG_DIR/profiles" "$MASF_STATE_DIR" "$MASF_JOBS_DIR"
test -e "$MASF_CONFIG_DIR/pilot.toml" || install -m 600 examples/factory.toml "$MASF_CONFIG_DIR/pilot.toml"
test -e "$MASF_CONFIG_DIR/profiles/postgres.yaml" || install -m 600 examples/profiles/postgres.yaml "$MASF_CONFIG_DIR/profiles/postgres.yaml"
control=(docker compose --env-file /dev/null -p masf-control -f examples/docker/compose.yaml)
"${control[@]}" config --quiet
```

El controlador monta la configuración en `/etc/masf` para lectura y el directorio completo de estado en `/var/lib/masf` para que SQLite, sus ficheros auxiliares y la evidencia sobrevivan a la recreación del contenedor. `MASF_JOBS_DIR` se monta en **la misma ruta absoluta dentro y fuera del controlador**. Docker resuelve un bind mount en la máquina del daemon. Así el servicio `app` recibe el checkout de esa tarea incluso cuando el comando Compose parte del controlador. El operador concede a los UID del agente y de los servicios acceso solo a los archivos que precisan dentro de cada checkout. [Bind mounts de Docker](https://docs.docker.com/engine/storage/bind-mounts/).

Una vez construida la imagen del incremento, se podrán ejecutar los comandos siguientes en la misma sesión. Para consultar GitHub, el controlador recibe `GH_TOKEN` desde el entorno del operador. `doctor --offline` se podrá ejecutar sin token ni socket en el controlador. La [extensión de acceso al motor](../examples/docker/engine.yaml) se añade a partir de I2.

```bash
"${control[@]}" run --rm controller doctor --offline          # I1
"${control[@]}" run --rm controller scan --dry-run           # I1, con acceso a GitHub
export MASF_DOCKER_SOCKET="${MASF_DOCKER_SOCKET:-/var/run/docker.sock}"
export MASF_DOCKER_GID="$(stat -c %g "$MASF_DOCKER_SOCKET")"
control+=(-f examples/docker/engine.yaml)
"${control[@]}" config --quiet
"${control[@]}" run --rm controller run --once              # I2
"${control[@]}" up -d controller                            # I4
"${control[@]}" exec -T controller masf --config /etc/masf/pilot.toml status
```

El socket da al controlador de confianza capacidad para administrar ese Engine. El controlador lanza los contenedores de agentes y los servicios del proyecto como hermanos. Ninguno de ellos recibe el socket, el estado de MASF ni la credencial de GitHub. Cada adaptador recibe únicamente su autenticación y los checks usan credenciales desechables del proyecto. La instalación inicial cubre un Engine local con rutas y permisos comprobados. Un daemon remoto o con reasignación de UID necesita validar por separado el almacenamiento y los permisos. [Seguridad del daemon](https://docs.docker.com/engine/security/).

En la instalación nativa, `MASF_JOBS_DIR` permite elegir la misma ruta de trabajo. Su valor predeterminado será `${XDG_DATA_HOME:-$HOME/.local/share}/masf/jobs`. El controlador leerá la autenticación de cada proveedor desde la configuración privada del operador y entregará al contenedor del rol solo el dato que requiera su adaptador. Los archivos del TOML y el directorio completo de configuración quedan fuera de los contenedores de tarea.

## Registrar un proyecto que se ejecuta en contenedores

En `[[repositories]]`, el bloque opcional `[repositories.compose]` escoge el entorno de `setup` y `checks`. `files` es una lista ordenada de manifiestos y overrides del operador, relativa al directorio del TOML. `service` identifica el servicio de ejecución en el modelo combinado. Si se omite el bloque, las comprobaciones usan `runner_image` del repositorio o `runner.image` global. La asignación de proveedores es independiente del entorno de pruebas. [Dos registros completos](../examples/factory.toml).

Para incorporar un proyecto existente, se puede tomar su Compose de una revisión aprobada y adaptar una copia en la configuración central. Allí se revisan las actualizaciones. Por ejemplo, en Laravel se seleccionaría el servicio `app` de un perfil con su base de datos de pruebas y se registraría `checks = [["php", "artisan", "test"]]`. La fábrica utiliza una copia de trabajo del SHA y su propia base de datos. El stack que el desarrollador tenga levantado puede seguir funcionando.

Antes de crear recursos, el controlador aplicará las siguientes comprobaciones al perfil y al checkout.

- Resolver los archivos del perfil dentro del directorio privado de configuración, comprobar permisos y enlaces, registrar el contenido aprobado (incluidos overrides) y su huella por intento. Ejecutar Compose desde ese directorio de confianza con `-f` explícitos en orden, `--project-directory` explícito, `--env-file /dev/null` y un entorno de variables permitido. El controlador calcula `MASF_WORKSPACE` a partir de la copia del SHA. Ni un fichero `.env`, ni un manifiesto, ni una variable aportados por la rama o la issue pueden alterar cómo se lanza Compose.
- Validar el modelo efectivo antes de `up`. Cada proyecto usa red y volúmenes propios, puertos internos por nombre de servicio y, si hacen falta puertos en el host, asignación dinámica. Un `container_name` fijo, un puerto fijo del host, un recurso externo o con nombre global, un socket, accesos al host fuera del checkout o privilegios de host bloquean el perfil con un diagnóstico. En el piloto se usan ficheros autocontenidos, sin `include`, `extends`, `env_file` ni secretos del host. Se comprueban las rutas antes de dejar que Compose resuelva referencias externas. [Modelo de confianza de Compose](https://docs.docker.com/compose/trust-model/).
- El perfil puede montar el checkout en una imagen de pruebas conocida o construir la imagen del proyecto con un contexto limitado a esa copia. Una imagen construida para una verificación usa un nombre propio de la ejecución. La receta del proyecto se ejecuta con los permisos del constructor aislado y sin credenciales de publicación, sockets ni directorios de otros trabajos. Si el proyecto necesita dependencias o servicios externos, el operador los declara en su perfil de pruebas.

### Ciclo de una verificación

1. Preparar una copia limpia del SHA y reservar en SQLite un nombre Compose único entre instalaciones, trabajos, intentos y verificaciones, por ejemplo `masf-<instalacion>-<trabajo>-<verificacion>`. Guardar nombre, SHA, perfil efectivo y ruta antes de crear recursos. Base y candidato usan copias, proyectos y volúmenes independientes incluso en el mismo job.
2. Levantar el perfil con `docker compose … up -d --build --wait --wait-timeout N`. Los servicios de los que depende una prueba de disponibilidad declaran `healthcheck`. Aplicar también el plazo de etapa a la construcción, los comandos y las operaciones del cliente. Ejecutar cada lista de argumentos en orden con `docker compose … exec -T SERVICIO <argv>`, desde el `working_dir` del servicio. Conservar código de salida, comando, SHA y logs filtrados. Una dependencia que no arranca se registra como fallo de infraestructura. Un check fallido del candidato sigue las rondas previstas. [Inicio y espera](https://docs.docker.com/reference/cli/docker/compose/up/) · [Ejecución sin TTY](https://docs.docker.com/reference/cli/docker/compose/exec/).
3. En éxito, fallo, cancelación o recuperación tras caída, detener únicamente ese proyecto con `docker compose … down --volumes --remove-orphans`. Si el perfil construye una imagen, añadir `--rmi local` para retirar la imagen con nombre generado por Compose. Las imágenes del perfil con nombre explícito se conservan. Usar la configuración congelada y las etiquetas de proyecto para verificar la propiedad de los recursos. Si la limpieza falla, conservar el identificador y el error para reintentar. [Nombres de proyecto](https://docs.docker.com/compose/how-tos/project-name/) · [Retirada de recursos](https://docs.docker.com/reference/cli/docker/compose/down/).

## Probar el perfil sin instalar MASF

Desde la raíz del repositorio, consulta la ayuda con `bash examples/docker/smoke-profile.sh --help` (o `-h`). La ayuda se muestra sin acceso a Docker.

Desde un host Linux con Docker y Compose, `bash examples/docker/smoke-profile.sh` prueba el [perfil de PostgreSQL](../examples/profiles/postgres.yaml) sin credenciales de GitHub ni de IA. Usa la imagen `postgres:17-alpine` y la descarga si falta. Ejecuta `psql` desde `app` contra `db`, crea una tabla en dos proyectos simultáneos y escribe una fila en la base. Comprueba que el candidato empieza vacío y que la base conserva su fila. Al salir retira los dos proyectos y sus volúmenes. Las credenciales de ejemplo solo abren la base efímera de este perfil, que usa una red interna sin puertos publicados.

El [workflow del piloto](../.github/workflows/docker-pilot.yml) valida con `docker compose config` el manifiesto central solo y con la extensión de acceso al motor. Después ejecuta el mismo script en un runner Linux de GitHub al cambiar los manifiestos o la prueba.

## Autenticación y diagnóstico

El servicio arranca con la credencial de GitHub del controlador. Para cada proveedor asignado, exporta la ruta en el host del archivo de autenticación de su CLI antes de crear el controlador. El runner recibe ese archivo por separado y trabaja con una copia en memoria durante la etapa.

| Proveedor | Distribución Linux para la imagen | Archivo de autenticación en el host |
| --- | --- | --- |
| Codex | Binario independiente y sus auxiliares | `MASF_CODEX_AUTH=$HOME/.codex/auth.json` tras `codex login` |
| Cursor | Directorio de la versión instalada de Cursor Agent | `MASF_CURSOR_AUTH=$HOME/.config/cursor/auth.json` tras `agent login` con almacén de archivo |
| Claude Code | Instalación nativa de `claude` | `MASF_CLAUDE_AUTH=$HOME/.claude/.credentials.json` tras autenticar la CLI en Linux |

`doctor` comprueba la lectura en GitHub, Docker, Compose y la versión de cada ejecutable asignado. La disponibilidad efectiva de cada cuenta para generar una respuesta se registra al ejecutar el rol. Si los tokens caducan, renueva la sesión en el host.

Cuando Docker agota su reserva automática de redes, puedes asignar dos subredes libres a la prueba de PostgreSQL. Este ejemplo requiere que las dos subredes estén libres en el host y en el Engine.

```bash
MASF_SMOKE_BASE_SUBNET=10.253.248.0/28 MASF_SMOKE_CANDIDATE_SUBNET=10.253.248.16/28 bash examples/docker/smoke-profile.sh
```

Para verificar el motor desde Linux, instala el paquete en un entorno virtual con `python -m pip install .`, ejecuta `python -m unittest discover -v` y activa las pruebas de Docker con `MASF_TEST_ENGINE=1 python -m unittest tests.test_engine -v`. Estas últimas aceptan las mismas dos variables de subred. La alternativa nativa incluye un [servicio de usuario de systemd](../examples/systemd/masf.service). Instala el entorno virtual en la ruta de su `ExecStart`, configura las variables de autenticación en `~/.config/masf/service.env` con permisos 0600, copia la unidad a `~/.config/systemd/user/` y ejecuta `systemctl --user enable --now masf`. Para mantenerlo tras el cierre de sesión, habilita la persistencia del gestor de usuario con `loginctl enable-linger "$USER"`.

## Operación continua y reanudación

La imagen de runtime incluye PyYAML, requerido por el paquete y sus pruebas. Después de actualizar MASF, reconstruye también el runner para que los checks del nuevo código dispongan de sus dependencias. Comprueba la rama base con ese runner antes de admitir issues.

Un fallo temporal reconocido durante los checks conserva el candidato y programa un reintento. Un resultado funcional fallido permite otra ronda de construcción dentro del límite configurado. Un error permanente o el presupuesto agotado dejan el trabajo bloqueado. Tras corregir la causa, `resume OWNER/REPO#N` repite la etapa correspondiente. El plazo `step_timeout_seconds` comprende el arranque del entorno y la suma de los comandos de preparación y comprobación. La limpieza dispone de su propio plazo para retirar los recursos.

Durante una etapa activa se consulta la issue cada cinco segundos, sujeto al tiempo de respuesta de GitHub. Si se cierra, pierde la etiqueta o cambia su contenido, se interrumpe el proceso y se retiran los recursos de comprobación. El estado registrado distingue cancelación de una petición que requiere confirmar el nuevo alcance.

Al recuperar una publicación, se busca primero el PR creado por ese trabajo y se contrasta su SHA con la evidencia guardada. Una rama base que haya avanzado después de publicar conserva el PR recuperado. Cuando solo se alcanzó a subir la rama, su intención persistida permite actualizarla con una condición de SHA para repetir las verificaciones antes de abrir el PR.

El sondeo respeta `Retry-After` y el reinicio de la cuota comunicado por GitHub. Un fallo aislado al consultar un PR se registra y permite atender otros trabajos. La autenticación caducada del proveedor se muestra como bloqueo y requiere renovar su acceso en el host antes de reanudar.

## Autorizar una ruta protegida para un trabajo

Cuando un candidato modifica una ruta protegida, `status` muestra el archivo y el trabajo pasa a `blocked`. Revisa el diff y, con autorización del operador, utiliza una ruta exacta al reanudar.

```bash
"${control[@]}" run --rm controller resume OWNER/REPO#N --allow-protected-path .github/workflows/factory.yml
```

La opción se puede repetir para varios archivos. Cada autorización queda asociada al trabajo y a la huella vigente de la issue, con fecha y SHA del candidato autorizado. El servicio comprueba las demás rutas protegidas usando el perfil del trabajo. Un cambio del contenido de la issue requiere confirmar el alcance y volver a autorizar las rutas necesarias. La reanudación conserva el candidato y continúa desde la etapa bloqueada.

## Recuperar una construcción interrumpida

El controlador registra la copia de construcción antes de llamar al agente. Si la etapa termina por timeout, `resume OWNER/REPO#N` reutiliza esa copia cuando corresponde al SHA de entrada y a la huella vigente de la issue. El siguiente intento recibe el plan y el motivo de la interrupción y continúa sobre los archivos conservados. Cada intento cuenta dentro del límite de rondas configurado.

Para trabajos creados por versiones anteriores que carezcan de esa referencia, el operador puede seleccionar una copia conservada dentro del directorio del trabajo.

```bash
"${control[@]}" run --rm controller resume OWNER/REPO#N --reuse-workspace /ruta/jobs/ID-N/tmpXXXX
```

La ruta debe ser un directorio directo del trabajo. El controlador valida su pertenencia y conserva el aislamiento de los metadatos Git. Si la tarea necesita más tiempo, ajusta `service.step_timeout_seconds` en el TOML antes de reanudarla.

## Diagnosticar una etapa de agente

Cada etapa conserva un archivo `<rol>-<id>.activity.jsonl` en su directorio de evidencia. Registra versión de CLI, tiempos y metadatos de actividad, como inicio y final de herramientas o resultado, asociados al intento y SHA. Los logs del controlador muestran una síntesis periódica de esa actividad.

Cursor se ejecuta con salida `stream-json`. El adaptador exige un resultado terminal completo y valida su contrato. Los fallos conservan la referencia a la actividad y la versión conocida, de forma que un timeout permita contrastar la última señal recibida. El contenido de argumentos, salidas de herramientas y deltas internos queda limitado al transporte temporal del proceso; la actividad persistida contiene metadatos operativos.

Al actualizar una CLI en el host, reconstruye el runner con `bash examples/docker/build-runner.sh codex cursor` para que el servicio utilice esa distribución.

El modo no interactivo de Cursor permite ejecutar las herramientas necesarias durante la revisión; Docker monta el código para lectura en los roles plan y review. El encargo se presenta con metadatos legibles y el diff en líneas independientes para que las herramientas de lectura puedan inspeccionarlo sin truncar una única línea JSON extensa.

## Resolver un conflicto de actualización de base

Si la rama base avanza y Git detecta un conflicto, el servicio registra las rutas y devuelve el árbol combinado al implementador dentro del presupuesto de recuperación. El nuevo candidato incluye ambos padres y necesita checks y revisión propios. Cuando se agota el presupuesto o es necesaria una decisión humana, el operador puede resolver el merge en un clon independiente y traer el commit resultante al almacén Git del trabajo.

El commit de resolución debe incluir como antecesores tanto el candidato conservado como la base actual. Tras revisar la resolución, se puede seleccionar con el comando siguiente.

```bash
"${control[@]}" run --rm controller resume OWNER/REPO#N --resolved-commit SHA_COMPLETO
```

Esta operación se aplica a una publicación bloqueada antes de registrar un PR. Conserva el plan y las rondas, registra los SHAs anteriores y la resolución, e inicia la comprobación de la nueva base y del candidato resuelto. El nuevo SHA recibe checks y revisión antes de publicarse.

## Recuperación autónoma limitada

El servicio utiliza estos valores opcionales de `[service]`. Las instalaciones existentes reciben los mismos valores por defecto.

```toml
max_recovery_attempts = 2
recovery_backoff_seconds = 30
```

El presupuesto se comparte entre los reintentos de errores temporales, actualizaciones de base y resoluciones de conflictos del trabajo. Cada decisión se conserva en `recoveries` y en los eventos de SQLite. `max_recovery_attempts = 0` desactiva las recuperaciones automáticas. Los permisos, proveedor asignado y plazo máximo de etapa siguen siendo los del perfil del operador.

Los timeouts, salidas de proveedor inválidas o incompletas y errores reconocidos de red o disponibilidad pueden pasar a `retry_wait`. La espera crece exponencialmente y respeta `Retry-After` o el reinicio de cuota de GitHub. `status` muestra la fecha UTC del siguiente intento. El siguiente sondeo disponible reclama el trabajo al cumplirse esa fecha, incluso después de reiniciar el controlador. Otros trabajos pueden ocupar su plaza durante la espera.

Una construcción interrumpida reutiliza su copia del mismo SHA y alcance. Su continuación cuenta como intento adicional de recuperación y conserva la cuenta total de construcciones. Una revisión interrumpida conserva candidato y checks válidos y repite únicamente la revisión. La publicación conserva su protocolo de reconciliación para recuperar el PR único tras una respuesta perdida.

Una pregunta o cambio de alcance, una ruta protegida, acceso denegado o presupuesto agotado requieren al operador. La recuperación automática no concede permisos ni modifica las credenciales o el límite de tiempo. Los errores desconocidos se conservan con su causa para su diagnóstico.
