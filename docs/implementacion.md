# Plan de implementación

Cada incremento deja algo que el operador puede comprobar y que el siguiente agente puede usar. Se empieza por descubrir trabajo real con acceso de lectura. La primera ejecución de pago llega cuando se puede entregar y revisar un PR completo.

## I1 · Registrar repositorios y descubrir trabajo

**Resultado para el operador.** Desde un clon de MASF se instala el paquete y se puede ver qué issues procesaría la fábrica y cuáles están en cola. Basta Python 3.11 o superior, SQLite y, para consultar GitHub, una CLI `gh` autenticada con lectura de los repositorios elegidos.

**Entrega.** Añadir `pyproject.toml` con el ejecutable `masf`, carga y validación del [TOML](../examples/factory.toml), un adaptador de lectura de GitHub y la primera migración de SQLite. La configuración por defecto vive en `$XDG_CONFIG_HOME/masf/config.toml` y el estado en `$XDG_STATE_HOME/masf/`. Cuando esas variables están vacías se utilizan `~/.config` y `~/.local/state` respectivamente. El parámetro global `--config` permite seleccionar otro archivo.

| Comando previsto | Efecto verificable |
| --- | --- |
| `masf --config FILE doctor --offline` | Valida estructura, rutas de roles, repositorios y límites sin acceder a la red. Distingue un proveedor deshabilitado de un error de configuración. |
| `masf --config FILE scan --dry-run` | Consulta repositorios habilitados y lista las issues elegibles con su motivo. El estado local y GitHub quedan intactos. |
| `masf --config FILE scan` | Inserta cada issue elegible una vez y guarda su identidad e instantánea inicial. Repetirlo informa de lo que ya está registrado. |
| `masf --config FILE status` | Muestra repositorio, número de issue y estado. Un registro recién creado figura como `queued`. |

**Aceptación.** En una instalación limpia del paquete, la configuración de ejemplo pasa la validación. Con un GitHub simulado que tenga dos páginas, una issue sin etiqueta, un PR etiquetado y dos repositorios con el mismo número de issue, solo se admiten las issues elegibles de los repositorios habilitados. Dos procesos que ejecuten `scan` sobre la misma base dejan una sola fila por issue. Un error de acceso se distingue de una cola vacía y permite que los otros repositorios se procesen. La suite se puede ejecutar sin cuentas de IA, Docker ni credenciales de escritura.

## I2 · Entregar el primer PR con Codex

**Resultado para el operador.** `masf --config FILE run --once` descubre trabajo y ejecuta o reanuda como máximo una issue. Al principio se asignan los tres roles a Codex. Se incorporan el runner aislado, la copia de trabajo, los checks sobre la base y el candidato, el registro por etapa y el publicador de GitHub. `masf --config FILE resume OWNER/REPO#N` devuelve a la cola un trabajo bloqueado cuya causa se ha atendido. La instalación documenta cómo construir localmente la imagen del runner y autenticar el proveedor elegido. `doctor` sin `--offline` comprueba el acceso de lectura a GitHub y las dependencias del runner e informa de aquello que solo puede verificarse con una ejecución real.

**Aceptación.** Un proveedor de prueba y un remoto Git temporal reproducen el camino desde `queued` hasta un único PR simulado. Cubrir un check fallido seguido de corrección, una revisión que pide cambios, una ruta protegida y una caída entre crear el PR y persistir su número. Solo se publica el SHA verificado. En el repositorio desechable del [piloto](piloto.md) se comprueba el recorrido con Codex real, y se registra la URL y el SHA. Esa prueba de campo requiere el acceso del operador y se informa por separado de la suite.

Simular también lectura correcta de la issue y un 403 al crear la rama o el PR. `doctor` debe distinguir los permisos observados de los desconocidos. Corregido el permiso, `resume` debe poder publicar el mismo SHA sin otra llamada de IA si siguen vigentes los checks y la revisión.

## I3 · Intercambiar los tres proveedores

**Resultado para el operador.** Añadir Cursor CLI y Claude Code como adaptadores del mismo contrato. `plan`, `build` y `review` se asignan desde la configuración, con excepciones por repositorio. Un proveedor deshabilitado o sin roles asignados puede estar ausente de la instalación. La preparación del runner y las instrucciones de autenticación cubren los tres proveedores de forma independiente.

**Aceptación.** Cada adaptador supera el mismo conjunto de casos de salida válida, salida inválida, tiempo agotado y fallo del proceso. Las muestras de las CLIs llevan versión y carecen de credenciales. Una ejecución mezcla al menos dos proveedores y el revisor recibe el diff y los checks aunque el implementador pertenezca a otro proveedor. Se registra por separado qué combinaciones se ejercitaron con cuentas reales y cuáles con dobles de prueba. Si la CLI devuelve un error de acceso o cuota, se conserva el trabajo y su causa para que pueda reanudarse.

## I4 · Instalar el servicio y ejecutar el piloto completo

**Resultado para el operador.** `masf --config FILE serve` sondea periódicamente y procesa la cola con los límites configurados. El servicio espacia las consultas ante límites de GitHub y recupera tareas tras reinicios. Se entregan instrucciones de instalación y actualización en Linux, un ejemplo opcional de servicio de usuario de systemd y la consulta de resultados mediante `status`.

**Aceptación.** Aplicar el protocolo del piloto desde un checkout limpio. La segunda consulta y el reinicio durante la publicación mantienen una única rama y un PR por issue. Un segundo repositorio funciona con la misma instalación y configuración adicional. El informe identifica la versión instalada, el destino, los agentes realmente ejecutados, los checks y las intervenciones humanas.

## Encargo para iniciar I1

> Trabaja en `yuloma/masf`. Lee `AGENTS.md`, `docs/especificacion.md`, `examples/factory.toml` y el apartado I1 de `docs/implementacion.md`. Implementa I1 en una rama del repositorio. Entrega un paquete instalable con los cuatro comandos descritos, persistencia idempotente y pruebas de aceptación que se ejecuten sin servicios externos. Usa la configuración de ejemplo como caso de prueba. Comprueba la instalación en un entorno virtual limpio y ejecuta la suite. Actualiza el README con los comandos que hayan quedado disponibles y deja un PR revisable con los resultados y cualquier limitación observada. El primer encargo termina cuando una issue elegible se puede registrar y consultar como `queued`.

Para preparar I2, el siguiente agente debe poder partir del paquete y la cola entregados en I1. Las decisiones nuevas que cambien el contrato público se reflejan en la especificación, el ejemplo y la aceptación del incremento correspondiente.
