# Trabajar en MASF

Estas instrucciones se aplican a quien desarrolla `yuloma/masf`. Las instrucciones de los agentes que procesan issues de otros proyectos son parte del producto y se versionan aquí con el motor.

- Lee la [especificación](docs/especificacion.md) para las reglas compartidas y el [plan](docs/implementacion.md) para el alcance y la aceptación de cada incremento. El primer encargo se puede copiar directamente del plan.
- Conserva una única instalación y el registro externo de repositorios. Al dar de alta un proyecto, aprovecha su CI, sus comandos y sus instrucciones existentes.
- Mantén las decisiones de estado, los reintentos, los checks y la publicación en código verificable. Los detalles propios de cada CLI pertenecen a su adaptador.
- Para desarrollar el motor, usa repositorios Git temporales, un proveedor de prueba y respuestas de GitHub simuladas. Las ejecuciones reales de proveedores se documentan por separado en el [piloto](docs/piloto.md).
- Escribe la documentación de uso en español. Usa inglés para identificadores y comentarios de código. Si cambias una opción, una transición o un comando público, actualiza en el mismo cambio su ejemplo y su criterio de aceptación.
- Antes de entregar un incremento, ejecuta sus comprobaciones y comunica el resultado observado y lo que quede pendiente. Durante la fase documental se pueden validar el TOML con `tomllib` de Python 3.11 o superior y el diff con `git diff --check`.
