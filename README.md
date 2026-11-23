# masf

Multi Agent Software Factory

MASF es una fábrica de software que se instala una vez y mantiene los repositorios de GitHub que el operador registra. Al encontrar una issue abierta con la etiqueta configurada, prepara un entorno para esa tarea, ejecuta los agentes elegidos, comprueba el cambio y entrega un pull request en borrador para revisión humana.

**El proyecto está en fase de especificación.** La fábrica, sus adaptadores y el instalador se desarrollarán en este mismo repositorio. Los comandos del piloto describen la interfaz prevista.

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
| Implementar el primer incremento y saber cuándo está terminado | [Plan de implementación y encargo inicial](docs/implementacion.md) |
| Instalar la primera versión y evaluar un PR real | [Protocolo de instalación y piloto](docs/piloto.md) |
| Contribuir a la propia fábrica con un agente | [AGENTS.md](AGENTS.md) |
