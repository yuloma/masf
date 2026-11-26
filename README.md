# masf

Multi Agent Software Factory

MASF es una fábrica de software que se instala una vez y mantiene los repositorios de GitHub que el operador registra. Al encontrar una issue abierta con la etiqueta configurada, prepara un entorno para esa tarea, ejecuta los agentes elegidos, comprueba el cambio y entrega un pull request en borrador para revisión humana.

## Instalar con un prompt

Abre Codex, Cursor o Claude Code con acceso a una terminal en la máquina Linux donde quieres alojar MASF. Pega este prompt.

```text
Instala en esta máquina la fábrica global de https://github.com/yuloma/masf. Sigue su INSTALL.md e implementa en MASF lo que falte.
Deja el servicio en marcha y dame el PR en borrador que la fábrica haya generado y verificado a partir de una issue de mi repositorio piloto.
```

Puedes añadir el `OWNER/REPO` del piloto al mensaje. Si abres el agente en un checkout de MASF, usará el manual de esa revisión. Para la prueba real necesitarás un repositorio donde puedas abrir un PR y acceso a una cuenta de al menos un proveedor. [Instrucciones de instalación](INSTALL.md).

**Estado de esta revisión.** El repositorio contiene la especificación y un piloto ejecutable de Docker. La primera ejecución del prompt incluye implementar el motor y los adaptadores pendientes. Cuando la revisión elegida ya incluya la fábrica completa, el mismo prompt instalará y verificará esa versión.

El prompt elige Docker Compose para el controlador central salvo que pidas la instalación nativa en Linux. En ambos casos, un proyecto que solo funciona en Docker podrá levantar su aplicación y sus dependencias para cada tarea. El perfil de PostgreSQL ya se puede probar con `bash examples/docker/smoke-profile.sh` en un host con Docker. [Despliegue y ejecución en Docker](docs/docker.md).

```text
Instala en esta máquina la fábrica global de https://github.com/yuloma/masf. Sigue su INSTALL.md e implementa en MASF lo que falte.
Deja el servicio en marcha y dame el PR en borrador que la fábrica haya generado y verificado a partir de una issue de mi repositorio piloto.
```

Puedes añadir el `OWNER/REPO` del piloto al mensaje. Si abres el agente en un checkout de MASF, usará el manual de esa revisión. Para la prueba real necesitarás un repositorio donde puedas abrir un PR y acceso a una cuenta de al menos un proveedor. [Instrucciones de instalación](INSTALL.md).

El prompt elige Docker Compose para el controlador central salvo que pidas la instalación nativa en Linux. En ambos casos, un proyecto que solo funciona en Docker podrá levantar su aplicación y sus dependencias para cada tarea. El perfil de PostgreSQL ya se puede probar con `bash examples/docker/smoke-profile.sh` en un host con Docker. [Despliegue y ejecución en Docker](docs/docker.md).

## Construir y operar el servicio

El [Makefile](Makefile) reúne las operaciones habituales sobre la instalación Compose existente. `make` muestra la ayuda; `make start` arranca la fábrica, `make status` consulta los trabajos y `make logs` muestra los logs. Desde un checkout limpio de `main`, con la cola en reposo, `make update` trae los últimos cambios de `origin/main`, reconstruye controlador y runner y recrea el servicio conservando el registro y el estado. Usa `MASF_PROVIDERS='codex cursor'` para elegir las CLIs del runner. [Recetas, variables y comprobaciones](docs/operacion.md).

Desde el checkout que se va a instalar, construye el controlador y empaqueta las distribuciones Linux independientes de las CLIs elegidas. El ejemplo utiliza Codex y Cursor.

```bash
docker build --target controller -t masf-controller:pilot .
bash examples/docker/build-runner.sh codex cursor
```

Prepara el registro externo y el array Bash `control` con ambos manifiestos. Activa el repositorio en el TOML y configura las rutas absolutas de autenticación que leerá Docker en el host. [Directorios y variables](docs/docker.md#desplegar-el-controlador).

```bash
export MASF_CODEX_AUTH="$HOME/.codex/auth.json"
export MASF_CURSOR_AUTH="$HOME/.config/cursor/auth.json"
export GH_TOKEN="$(gh auth token)"
"${control[@]}" run --rm controller doctor --offline
"${control[@]}" run --rm controller doctor
"${control[@]}" run --rm controller scan --dry-run
"${control[@]}" up -d controller
"${control[@]}" exec -T controller masf --config /etc/masf/pilot.toml status
"${control[@]}" logs --tail 40 controller
```

Para actualizar, construye las imágenes del controlador y del runner de la nueva revisión y ejecuta `"${control[@]}" up -d --force-recreate controller` cuando la cola esté en reposo. Los bind mounts conservan la base y los trabajos. Si un proveedor rechaza la autenticación, renueva el acceso de su CLI y usa `"${control[@]}" run --rm controller resume OWNER/REPO#N`. Una reasignación de roles en el TOML se aplica a una tarea bloqueada al reanudarla.

`serve` registra el CI por SHA de los PR abiertos en los repositorios habilitados. Usa `status --pr` para el head y los resultados actuales, y `logs --pr` para las transiciones, diagnósticos y SHA históricos. Para filtrar un head sustituye `HEAD_SHA` por su SHA completo. [Campos y valores de la observación](docs/especificacion.md#formato-de-observación-v1).

`masf --config FILE observe-api --port 8765` sirve el panel web en `http://127.0.0.1:8765/dashboard/`, con trabajos, intervenciones, PR y evidencia. Usa `--actions-token-file FILE` para tramitar decisiones desde el panel. [Arranque y comprobaciones](docs/dashboard.md). [Rutas, formatos y despliegue de solo lectura](docs/observacion.md).

```bash
masf --config FILE status --pr OWNER/REPO#23
masf --config FILE logs --pr OWNER/REPO#23 --sha HEAD_SHA
masf --config FILE link-pr OWNER/REPO#23 OWNER/REPO#10
"${control[@]}" exec -T controller masf --config /etc/masf/pilot.toml logs --pr OWNER/REPO#23
```

`link-pr` se usa cuando `status` muestra `job: null` y el operador identifica una issue ya registrada del mismo repositorio. En Docker se ejecuta con el mismo prefijo `exec -T controller masf --config /etc/masf/pilot.toml`.

## Revisión independiente de integración

El merge sigue siendo manual por defecto. Para incorporar un repositorio, configura `merge_review` y `repositories.merge_policy` en el [registro central de ejemplo](examples/factory.toml). Tras un CI correcto, una sesión nueva recibe el diff, el código de solo lectura, los criterios y la evidencia del PR. La política indica qué riesgos requieren revisión humana, qué comprobaciones deben pasar y si integrar el PR inicia un despliegue.

Cuando el controlador carece de una operación capaz de condicionar la integración a la base y al head evaluados, conserva el PR y solicita intervención. La operación de GitHub disponible para el controlador condiciona el head. Consulta el motivo, comprueba las revisiones vigentes y registra tu decisión antes de integrar manualmente en GitHub. Si obtienes evidencia nueva, puedes repetir la revisión.

```bash
masf --config FILE status --json
masf --config FILE history OWNER/REPO#10
masf --config FILE resume OWNER/REPO#10 --decision-context
masf --config FILE resume OWNER/REPO#10 --retry-merge-review --evidence 'CI de integración disponible'
masf --config FILE resume OWNER/REPO#10 --record-manual-merge --evidence 'Base, head y recuperación verificados'
```

## Cómo funciona

1. El operador registra el repositorio, su etiqueta, sus comprobaciones y los proveedores disponibles en la configuración central.
2. Una persona marca una issue que está lista para desarrollo. El servicio la recoge en el siguiente sondeo y abre un único trabajo para ella.
3. Un agente prepara el plan, otro puede implementarlo y un tercero puede revisar el diff. El operador puede asignar todos los roles a un proveedor o combinar Codex, Cursor y Claude Code.
4. Los PR publicados reciben observación de CI. Si el repositorio activa la etapa de integración, el controlador ejecuta la revisión independiente y expone la decisión y la evidencia a la persona que atiende la intervención.

## Documentos de trabajo

| Para | Leer |
| --- | --- |
| Llevar el encargo desde el checkout hasta un servicio activo y un PR real | [Instrucciones para el agente instalador](INSTALL.md) |
| Entender el ciclo de una issue, los permisos y el contrato de los agentes | [Especificación](docs/especificacion.md) |
| Ver cómo se registran repositorios y se asignan proveedores | [Configuración de ejemplo](examples/factory.toml) |
| Desplegar el controlador y configurar los servicios de un proyecto Docker | [Guía de Docker](docs/docker.md) · [Compose central](examples/docker/compose.yaml) · [Perfil de proyecto](examples/profiles/postgres.yaml) |
| Implementar una capacidad y comprobar su aceptación | [Plan de implementación por incrementos](docs/implementacion.md) |
| Instalar la primera versión y evaluar un PR real | [Protocolo de instalación y piloto](docs/piloto.md) |
| Contribuir a la propia fábrica con un agente | [AGENTS.md](AGENTS.md) |
