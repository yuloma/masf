# Recetas de operación con Make

Desde un checkout de MASF, `make` o `make help` muestra las recetas disponibles sin consultar Docker ni exigir una instalación. Se necesita GNU Make y Bash. Estas recetas operan el controlador de Docker Compose; para el servicio nativo se mantienen los comandos de systemd de la [guía de Docker](docker.md#autenticación-y-diagnóstico).

## Usar la instalación existente

Si existe `~/.local/share/masf-pilot/control`, las recetas lo reutilizan, incluidos sus manifiestos, credenciales y overrides. `MASF_ROOT` permite cambiar ese directorio; `MASF_CONTROL` selecciona explícitamente otro ejecutable compatible con `docker compose`. Es una ruta a un ejecutable, sin argumentos añadidos.

```bash
make start
make ps
make status
make logs
make logs-follow
make doctor-offline
make doctor
make scan
make stop
```

`start` deja el controlador en segundo plano con la política de reinicio de Compose. `stop` detiene únicamente ese servicio y conserva configuración, SQLite y trabajos. `restart` lo recrea con la imagen local vigente; úsalo con la cola en reposo. `status` necesita el controlador en marcha; los diagnósticos y `scan` usan un contenedor efímero. `scan` siempre ejecuta `--dry-run`, por lo que no admite nuevas tareas.

```bash
make status MASF_CONTROL=/ruta/privada/control
make start MASF_ROOT=/ruta/privada/masf
```

Cuando no existe un wrapper, se usan los manifiestos `examples/docker/compose.yaml` y `engine.yaml` del checkout, con `--env-file /dev/null`. Las variables exportadas en el host o pasadas a Make eligen las rutas existentes. Las recetas no crean directorios ni sobrescriben el registro: prepara primero la instalación siguiendo [INSTALL.md](../INSTALL.md).

| Variable | Valor predeterminado sin wrapper |
| --- | --- |
| `MASF_ROOT` | `~/.local/share/masf-pilot` |
| `MASF_CONFIG_DIR` | `$MASF_ROOT/config`; debe contener `pilot.toml` |
| `MASF_STATE_DIR` | `$MASF_ROOT/state` |
| `MASF_JOBS_DIR` | `$MASF_ROOT/jobs` |
| `MASF_PROJECT` | `masf-control`; conserva el nombre de tu instalación |
| `MASF_CONTROLLER_IMAGE` | `masf-controller:pilot` |
| `MASF_DOCKER_SOCKET` | `/var/run/docker.sock`; se selecciona explícitamente ese Engine local |
| `MASF_UID`, `MASF_GID` | UID y GID del usuario actual |
| `MASF_DOCKER_GID` | Grupo del socket seleccionado |

Conserva las rutas absolutas y permisos de la [guía de despliegue](docker.md#desplegar-el-controlador). Sin wrapper, se utiliza `GH_TOKEN`, después `GITHUB_TOKEN` y, si faltan ambos, `gh auth token`. Exporta las rutas `MASF_CODEX_AUTH`, `MASF_CURSOR_AUTH` y/o `MASF_CLAUDE_AUTH` de los proveedores elegidos antes de arrancar o recrear el controlador. Un wrapper propio conserva su forma de preparar el entorno y debe apuntar a la misma instalación e imagen que se construye.

## Construir y actualizar desde la rama principal

`build` construye el controlador y el runner del checkout actual. El controlador incluye el SHA en su etiqueta OCI de revisión. El runner empaqueta las CLIs Linux ya instaladas en el host: por defecto Codex y Cursor. Selecciona los proveedores que usa tu registro.

```bash
make build MASF_PROVIDERS='codex cursor'
make build-controller
make build-runner MASF_PROVIDERS=claude
```

El runner y su base conservan los nombres del constructor existente, `masf-runner:pilot` y `masf-runtime:pilot`. El registro debe usar esa imagen del runner. Las construcciones seleccionan el mismo socket local `MASF_DOCKER_SOCKET` (por defecto `/var/run/docker.sock`), también al usar un wrapper propio. Construir no recrea el controlador.

Para actualizar, usa el checkout de operación en `main`, sin cambios locales ni commits propios pendientes de integrar. Espera a que la cola esté en reposo antes de comenzar; `update` recrea el controlador y no implementa un drenaje de trabajos. Conserva las mismas variables, credenciales y wrapper que usas al arrancar.

```bash
make status
make update MASF_PROVIDERS='codex cursor'
make logs
```

`update` obtiene la rama `main` de `origin`, comprueba que el HEAD local sea su antecesor y aplica únicamente un avance rápido. Rechaza ramas de desarrollo, HEAD separado, cambios locales —también archivos sin seguimiento— y commits locales que no estén en la rama remota. No cambia de rama, descarta archivos ni crea commits de merge. `MASF_REMOTE` y `MASF_MAIN_BRANCH` permiten seleccionar otro remoto o el nombre de la rama principal.

Después vuelve a cargar el script de la revisión descargada, construye controlador y runner, valida el registro con `doctor --offline` y recrea solo `controller`. Finalmente muestra el contenedor y los trabajos. Si falla el fetch, la construcción o la validación, devuelve un error y no llega a recrear el servicio. Tras un fallo de construcción o validación el checkout ya puede estar actualizado y pueden haberse construido imágenes; corrige la causa y repite `make update`. Si falla el arranque, consulta `make ps` y `make logs`. Los bind mounts conservan el registro, el estado y las copias de trabajo durante la actualización.

## Comprobar los cambios

```bash
make check
make test
make smoke
make test-engine
```

`PYTHON=/ruta/al/venv/bin/python` selecciona el intérprete de `test` y `test-engine`; instala antes el paquete con sus dependencias en ese entorno. `test` usa GitHub y proveedores simulados, con las pruebas optativas de Engine desactivadas salvo que se activen en el entorno. `smoke` ejecuta la prueba independiente de aislamiento de PostgreSQL. `test-engine` activa las pruebas de checks y recuperación de procesos con Docker real y requiere las imágenes descritas en la [aceptación de I2](implementacion.md#i2--entregar-el-primer-pr-con-codex). Ambas recetas de Docker respetan las variables de subred de esa guía.

**Criterio de aceptación.** `python3 -m unittest tests.test_makefile -v` usa remotos Git temporales y ejecutables simulados, sin modificar el servicio real. Comprueba la ayuda sin instalación, la reutilización del wrapper, las rutas de Compose, las operaciones de servicio, el avance al SHA remoto y la conservación del registro y estado. Verifica el rechazo de cambios locales, ramas equivocadas, HEAD separado, commits locales y divergencias, y que un fallo de descarga, construcción o validación impida recrear el controlador. `make smoke` acredita por separado el aislamiento con un Engine local.
