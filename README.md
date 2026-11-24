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

## Cómo se usará

1. El operador registra el repositorio, su etiqueta, sus comprobaciones y los proveedores disponibles en la configuración central.
2. Una persona marca una issue que está lista para desarrollo. El servicio la recoge en el siguiente sondeo y abre un único trabajo para ella.
3. Un agente prepara el plan, otro puede implementarlo y un tercero puede revisar el diff. El operador puede asignar todos los roles a un proveedor o combinar Codex, Cursor y Claude Code.
4. La persona que revisa el PR recibe el cambio, los criterios de aceptación y el resultado de los checks. La incorporación de un repositorio requiere acceso a GitHub y una entrada en la configuración central, con cero archivos de MASF en el proyecto destino.

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
