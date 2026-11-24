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
3. En éxito, fallo, cancelación o recuperación tras caída, detener únicamente ese proyecto con `docker compose … down --volumes --remove-orphans`. Usar la configuración congelada y las etiquetas de proyecto para verificar la propiedad de los recursos. Si la limpieza falla, conservar el identificador y el error para reintentar. El controlador puede reconciliar su estado tras reiniciarse sin borrar volúmenes de desarrollo ni de otros trabajos. [Nombres de proyecto](https://docs.docker.com/compose/how-tos/project-name/) · [Retirada de recursos](https://docs.docker.com/reference/cli/docker/compose/down/).

## Probar el perfil sin instalar MASF

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
