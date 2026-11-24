# Instalación y piloto

Este protocolo se ejecutará con una revisión que ya implemente el incremento correspondiente. Los comandos `masf` de este documento son la interfaz prevista. El primer resultado de campo será un PR en un proyecto desechable elegido por el operador.

Para la instalación completa desde el prompt del README, [INSTALL.md](../INSTALL.md) indica cuándo construir las piezas pendientes, cómo poner en marcha el servicio y qué evidencia entregar. Los ejercicios de este protocolo también se pueden ejecutar por separado durante el desarrollo.

## 1. Preparar la instalación central

Para el piloto recomendado usa un host Linux con Docker Engine y Compose. El controlador también se podrá instalar directamente en Linux con Python 3.11 o superior y Git. En ambas modalidades, I1 requiere acceso de lectura a GitHub para el sondeo. Desde I2 se necesita Docker, permiso para subir una rama y crear un PR en el proyecto, y acceso a cada proveedor que tenga un rol asignado.

Trabaja en un clon de `yuloma/masf`. Selecciona la versión a evaluar y guarda el resultado de `git rev-parse HEAD`. En Docker, construye la imagen documentada en el README de esa versión, prepara las rutas y la variable `control` de la [guía de despliegue](docker.md#desplegar-el-controlador) y usa el siguiente atajo con los comandos disponibles del incremento. Desde I2 incorpora la extensión del motor a `control` como indica la guía. La configuración que se edita en el host es `$MASF_CONFIG_DIR/pilot.toml`.

```bash
masf_pilot() { "${control[@]}" run --rm controller "$@"; }
masf_pilot doctor --offline
```

Para evaluar la alternativa nativa, prepara el paquete y el mismo perfil de proyecto en otra instalación.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
masf_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/masf"
install -d -m 700 "$masf_config_dir" "$masf_config_dir/profiles"
masf_config="$masf_config_dir/pilot.toml"
test -e "$masf_config" || install -m 600 examples/factory.toml "$masf_config"
test -e "$masf_config_dir/profiles/postgres.yaml" || install -m 600 examples/profiles/postgres.yaml "$masf_config_dir/profiles/postgres.yaml"
masf_pilot() { masf --config "$masf_config" "$@"; }
masf_pilot doctor --offline
```

A partir de I2, construye la imagen del runner según el README de la versión instalada. Configura GitHub en el servicio y autentica las CLIs seleccionadas en el entorno en que se ejecutarán. El modo de acceso y las cuotas dependen de las cuentas del operador. `doctor` debe informar de binarios, versiones y dependencias comprobables sin iniciar una tarea de pago.

## 2. Dar de alta el destino

1. Elige un repo propio de pruebas con una rama predeterminada y anota el SHA inicial. Como caso mínimo, usa `saludo.py` con una función `saludo(nombre)` que devuelva `f"Hola, {nombre}"` y una prueba existente en `tests/test_saludo.py` para la entrada `"Ana"`.
2. Verifica sobre esa base que `python3 -m unittest discover -s tests` ejecuta al menos una prueba y pasa. Es el comando del perfil de ejemplo.
3. En `pilot.toml` sustituye `example-org/masf-pilot` por el destino real y activa `enabled` de esa entrada. Elige una etiqueta existente o crea `masf:ready` en GitHub. El alta solo requiere esa configuración y los permisos del repositorio.
4. En I2 asigna `plan`, `build` y `review` a `codex`. Desde I3 puedes usar Codex para planificar e implementar y Cursor para revisar, tal como indica el ejemplo. Para una ejecución con los tres, habilita Claude Code y asigna `plan = "claude"` a ese repositorio. Guarda las credenciales fuera del TOML.
5. Comprueba `masf_pilot doctor` cuando esté disponible. Anota qué proveedores se usarán. Un proveedor pendiente de credenciales puede quedar deshabilitado.
6. Comprueba que la identidad que publicará tiene acceso al destino elegido. Registra por separado la lectura de issues, la escritura de ramas y la creación de PR. En una instalación nueva, la primera publicación real confirma las dos últimas operaciones.

## 3. Ejecutar una tarea observable

Crea una issue en el repositorio de pruebas con el siguiente encargo.

> Ajusta `saludo(nombre)` para quitar espacios de los extremos. Con `"  Ana  "` debe devolver `"Hola, Ana"`, con `"   "` debe devolver `"Hola, mundo"` y con `"Eva"` debe seguir devolviendo `"Hola, Eva"`. Añade pruebas para esos comportamientos.

Ejecuta `scan --dry-run` antes y después de añadir la etiqueta. La issue debe aparecer únicamente después de etiquetarla. Con dos ejecuciones de `scan` debe figurar una sola vez en `status`. Desde I2, `run --once` ejecuta el primer trabajo manual.

```bash
masf_pilot scan --dry-run
masf_pilot scan
masf_pilot scan
masf_pilot status
masf_pilot run --once
masf_pilot status
```

Abre el PR en borrador. Comprueba los tres ejemplos en el SHA publicado, revisa el diff, verifica qué proveedor hizo cada etapa y contrasta el resultado de los checks con el del PR. La rama base debe conservar su SHA en este repo de prueba hasta que una persona decida fusionar. El diff debe contener únicamente los cambios propios de la tarea.

Desde I4 arranca el controlador con `"${control[@]}" up -d controller` en Docker, o `masf_pilot serve` en la instalación nativa. Etiqueta una **segunda issue** independiente, por ejemplo añadir al README del proyecto un ejemplo de uso de `saludo("Eva")` con su salida. El servicio ha de recogerla sin una orden manual. En Docker, ejecuta `"${control[@]}" up -d --force-recreate controller` y verifica con `masf_pilot status` que se conservan los identificadores y el PR. Los casos de caída en el punto exacto de publicación se reproducen mediante la prueba controlada de I2.

### Segundo destino con aplicación y base de datos en Docker

1. En el mismo host, ejecuta `bash examples/docker/smoke-profile.sh`. Este ejercicio ya puede ejecutarse sin el motor de MASF. Comprueba que salen con éxito los comandos SQL del servicio `app` y que el proyecto candidato tiene datos independientes del de la base. [Perfil utilizado](../examples/profiles/postgres.yaml).
2. Con I2 implementado, registra un segundo repo de prueba mediante la entrada `example-org/masf-pilot-sql`. Usa un proyecto con `schema.sql` que cree `probe(id integer PRIMARY KEY)` y `tests/smoke.sql` que falle si la tabla contiene filas, como en el script. Estos archivos son el código y las pruebas del proyecto. El TOML y los manifiestos siempre están en la instalación central.
3. Etiqueta una issue que pida añadir a `probe` una columna `label` obligatoria, con valor inicial `'nuevo'`, y comprobarlo en su prueba SQL. Ejecuta `masf_pilot run --once` con esa issue como único trabajo elegible pendiente. En `status` y en la evidencia del mismo SHA deben constar el servicio `app`, el resultado de `psql` y proyectos Compose distintos para base y candidato.
4. Mantén otro stack de desarrollo abierto con un dato de control. Comprueba que conserva el dato y sus servicios después de una ejecución y de cancelar otra. Tras recrear el controlador en mitad de una verificación, debe recuperar la referencia, retirar solo los recursos de esa verificación y permitir terminar el trabajo. Anota cualquier red o volumen huérfano.

## 4. Decidir si ampliar el uso

| Evidencia | Criterio para el piloto |
| --- | --- |
| Descubrimiento y recuperación | Dos sondeos y un reinicio dejan el mismo id de trabajo y un único PR por issue. |
| Alcance del cambio | Los tres comportamientos se verifican en el SHA del PR. El repo destino conserva su estructura y la decisión de merge. |
| Intercambio de proveedores | El plan y el diff pasan entre al menos dos CLIs reales. El registro identifica por separado cualquier adaptador que solo se ejercitó con un doble de prueba. |
| Fallos | Un check fallido impide la publicación y deja evidencia. El número de rondas se mantiene dentro del límite. Un fallo de autenticación indica qué proveedor necesita atención. |
| Alta de un segundo proyecto | Una segunda entrada en el TOML basta para descubrir su issue. Un número de issue coincidente en ambos repos produce trabajos distintos. |
| Proyecto Docker | Los checks corren en el servicio configurado y se conserva su código de salida. Los datos de la base, del candidato y del stack de desarrollo siguen siendo independientes. |
| Persistencia y limpieza | Recrear el controlador conserva SQLite, evidencia e identificadores. Terminar o cancelar una prueba retira sus contenedores, redes y volúmenes propios. |
| Coste del proceso | Quedan anotados tiempo por etapa, rondas, intervenciones y uso disponible. Si no hay datos de coste, el informe lo indica. |

Conserva en la instalación del operador la revisión de MASF, versiones de las CLIs ejecutadas, id del trabajo, SHA, URL del PR y el resultado de cada criterio. En una instalación operativa, deja `serve` activo con los repositorios autorizados. Si el operador solicitó únicamente un ensayo desechable, detén su instancia y deshabilita sus entradas de prueba al terminar.

## Evidencia de recuperación autónoma del 24 de noviembre de 2026

En `yuloma/masf`, el candidato resuelto de #24 pasó checks y revisión real de Cursor antes de que la fábrica publicara el borrador [PR #30](https://github.com/yuloma/masf/pull/30). La recuperación autónoma posterior se entrega en el [PR #29](https://github.com/yuloma/masf/pull/29), que depende de #27.

La revisión `9ea085d0b3cb97cc7ea5942469cb70e1e7d2cc71` pasó 78 pruebas en el host y en la imagen del controlador con Python 3.11, con nueve casos optativos de Engine omitidos en esas suites. Los diez casos de `tests.test_engine` se comprobaron por separado, además del smoke de PostgreSQL. Ocho pasaron directamente con las subredes del smoke. Los dos perfiles privados se repitieron con subredes explícitas en sus archivos temporales porque el host había agotado sus pools automáticos. Pasaron conservando la fila del stack de control y retirando los recursos de cada intento.

Los timeouts, pérdida de respuesta, reinicios y conflictos de Git se provocaron con remotos temporales, proveedores simulados y respuestas de GitHub simuladas. Cubren reanudación con un Worker nuevo, presupuesto persistente, espera sin ocupar capacidad, conservación de archivos, repetición de revisión sobre el mismo SHA, cancelación y publicación de un PR único.

Cursor Agent `2026.11.22-e9042b2` revisó realmente la implementación `51f338905ecbc645789b3805c7885ff14db981fd` con el código montado para lectura y devolvió `pass` sin hallazgos en 143,84 segundos. El registro de instalación del operador conserva por separado los logs de aceptación y las revisiones posteriores con su SHA y versión de CLI.
