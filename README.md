# masf

Multi Agent Software Factory

MASF es una fábrica de software que se instala una vez y mantiene los repositorios de GitHub que el operador registra. Al encontrar una issue abierta con la etiqueta configurada, prepara un entorno para esa tarea, ejecuta los agentes elegidos, comprueba el cambio y entrega un pull request en borrador para revisión humana.

**El proyecto está en fase de especificación.** La fábrica, sus adaptadores y el instalador se desarrollarán en este mismo repositorio. Los comandos del piloto describen la interfaz prevista.

La instalación recomendada usará Docker Compose para el controlador central. También se podrá ejecutar el controlador nativamente en Linux. En ambos casos, un proyecto que solo funciona en Docker podrá levantar su aplicación y sus dependencias para cada tarea, conservando el entorno de desarrollo del equipo. El perfil de PostgreSQL ya se puede probar con `bash examples/docker/smoke-profile.sh` en un host con Docker. [Despliegue y ejecución en Docker](docs/docker.md).

## Cómo se usará

1. El operador registra el repositorio, su etiqueta, sus comprobaciones y los proveedores disponibles en la configuración central.
2. Una persona marca una issue que está lista para desarrollo. El servicio la recoge en el siguiente sondeo y abre un único trabajo para ella.
3. Un agente prepara el plan, otro puede implementarlo y un tercero puede revisar el diff. El operador puede asignar todos los roles a un proveedor o combinar Codex, Cursor y Claude Code.
4. La persona que revisa el PR recibe el cambio, los criterios de aceptación y el resultado de los checks. La incorporación de un repositorio requiere acceso a GitHub y una entrada en la configuración central, con cero archivos de MASF en el proyecto destino.

## Documentos de trabajo

| Para | Leer |
| --- | --- |
| Entender el ciclo de una issue, los permisos y el contrato de los agentes | [Especificación](docs/especificacion.md) |
| Ver cómo se registran repositorios y se asignan proveedores | [Configuración de ejemplo](examples/factory.toml) |
| Desplegar el controlador y configurar los servicios de un proyecto Docker | [Guía de Docker](docs/docker.md) · [Compose central](examples/docker/compose.yaml) · [Perfil de proyecto](examples/profiles/postgres.yaml) |
| Implementar el primer incremento y saber cuándo está terminado | [Plan de implementación y encargo inicial](docs/implementacion.md) |
| Instalar la primera versión y evaluar un PR real | [Protocolo de instalación y piloto](docs/piloto.md) |
| Contribuir a la propia fábrica con un agente | [AGENTS.md](AGENTS.md) |
