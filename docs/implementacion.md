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

**Aceptación.** En una instalación limpia del paquete, la configuración de ejemplo pasa la validación. Con un GitHub simulado que tenga dos páginas, una issue sin etiqueta, un PR etiquetado y dos repositorios con el mismo número de issue, solo se admiten las issues elegibles de los repositorios habilitados. Dos procesos que ejecuten `scan` sobre la misma base dejan una sola fila por issue. Un error de acceso se distingue de una cola vacía y permite que los otros repositorios se procesen. La suite se puede ejecutar sin cuentas de IA, Docker ni credenciales de escritura.

Como prueba de empaquetado en un host con Docker, construir la imagen, ejecutar `doctor --offline` sin montar el socket y comprobar que un `scan` y un `status` en contenedores distintos conservan la misma issue en SQLite. Ejercitar en la suite nativa una entrada histórica sin bloque Compose, la entrada multiservicio de ejemplo y una ruta que intenta salir del directorio de configuración.

## I2 · Entregar el primer PR con Codex

**Resultado para el operador.** `masf --config FILE run --once` descubre trabajo y ejecuta o reanuda como máximo una issue. Al principio se asignan los tres roles a Codex. Se incorporan el runner aislado, la copia de trabajo, los checks sobre la base y el candidato, el registro por etapa y el publicador de GitHub. `masf --config FILE resume OWNER/REPO#N` devuelve a la cola un trabajo bloqueado cuya causa se ha atendido. La instalación documenta cómo construir localmente la imagen del runner y autenticar el proveedor elegido. `doctor` sin `--offline` comprueba el acceso de lectura a GitHub y las dependencias del runner e informa de aquello que solo puede verificarse con una ejecución real.

**Aceptación.** Un proveedor de prueba y un remoto Git temporal reproducen el camino desde `queued` hasta un único PR simulado. Cubrir un check fallido seguido de corrección, una revisión que pide cambios, una ruta protegida y una caída entre crear el PR y persistir su número. Solo se publica el SHA verificado. En el repositorio desechable del [piloto](piloto.md) se comprueba el recorrido con Codex real, y se registra la URL y el SHA. Esa prueba de campo requiere el acceso del operador y se informa por separado de la suite.

Simular también lectura correcta de la issue y un 403 al crear la rama o el PR. `doctor` debe distinguir los permisos observados de los desconocidos. Corregido el permiso, `resume` debe poder publicar el mismo SHA sin otra llamada de IA si siguen vigentes los checks y la revisión.

**Ejecución en Docker.** Incorporar al controlador el cliente Docker y Compose y conectar el socket solo mediante la [extensión del motor](../examples/docker/engine.yaml). Implementar los modos de imagen individual y `repositories.compose` del [contrato](docker.md), incluidos el límite de tiempo, el código de salida de cada comando, la copia limpia por SHA, la persistencia del nombre de proyecto y la recuperación de recursos.

**Aceptación con Engine local.** Ejecutar primero la [prueba independiente de PostgreSQL](../examples/docker/smoke-profile.sh). Repetir a través de MASF con el perfil de ejemplo y sin herramientas del proyecto en el host. Verificar la misma tarea con controlador nativo y contenedorizado en instalaciones de prueba independientes. Cubrir dos verificaciones simultáneas, servicios no saludables, un check con salida distinta de cero, cancelación y recuperación después de crear un volumen. Comprobar que un manifiesto o `.env` introducido en la rama no cambia el despliegue, que un perfil con un montaje del socket se rechaza antes de `up` y que el stack de desarrollo sobrevive a la limpieza. Registrar por separado la prueba con Engine de las pruebas con dobles.

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
