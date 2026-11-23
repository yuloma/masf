# Trabajar en MASF

Estas instrucciones se aplican a quien desarrolla `yuloma/masf`. Las instrucciones de los agentes que procesan issues de otros proyectos son parte del producto y se versionan aquí con el motor.

- Si el operador pide instalar la fábrica con el prompt del README, sigue [INSTALL.md](INSTALL.md) hasta dejar el servicio activo y verificar el PR del piloto. Lee la [especificación](docs/especificacion.md) para las reglas compartidas y el [plan](docs/implementacion.md) para las aceptaciones de cada incremento. El encargo individual de I1 se utiliza cuando el operador pide específicamente ese incremento.
- Conserva una única instalación y el registro externo de repositorios. Al dar de alta un proyecto, aprovecha su CI, sus comandos y sus instrucciones existentes.
- Consulta el [contrato de Docker](docs/docker.md) al cambiar el despliegue o la ejecución de checks. Ambas instalaciones usan el mismo formato de configuración y estado. El socket queda reservado al controlador. Prueba el aislamiento con `bash examples/docker/smoke-profile.sh` cuando dispongas de un Engine local.
- Mantén las decisiones de estado, los reintentos, los checks y la publicación en código verificable. Los detalles propios de cada CLI pertenecen a su adaptador.
- Para desarrollar el motor, usa repositorios Git temporales, un proveedor de prueba y respuestas de GitHub simuladas. Las ejecuciones reales de proveedores se documentan por separado en el [piloto](docs/piloto.md).
- Escribe la documentación de uso en español. Usa inglés para identificadores y comentarios de código. Si cambias una opción, una transición o un comando público, actualiza en el mismo cambio su ejemplo y su criterio de aceptación.
- Antes de entregar un incremento, ejecuta sus comprobaciones y comunica el resultado observado y lo que quede pendiente. Durante la fase documental se pueden validar el TOML con `tomllib` de Python 3.11 o superior y el diff con `git diff --check`.
