# Instalación y piloto

Este protocolo se ejecutará con una revisión que ya implemente el incremento correspondiente. Los comandos `masf` de este documento son la interfaz prevista. El primer resultado de campo será un PR en un proyecto desechable elegido por el operador.

## 1. Preparar la instalación central

Usa una máquina Linux con Python 3.11 o superior y Git. Para I1 se necesita `gh` con acceso de lectura al proyecto piloto. Desde I2 se necesita también Docker, permiso para subir una rama y crear un PR en ese proyecto, y acceso a cada proveedor que tenga un rol asignado.

Trabaja en un clon de `yuloma/masf`. Selecciona la versión a evaluar y guarda el resultado de `git rev-parse HEAD` junto a las observaciones. Estos comandos prepararán el paquete y una configuración local de prueba.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
masf_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/masf"
mkdir -p "$masf_config_dir"
masf_config="$masf_config_dir/pilot.toml"
test -e "$masf_config" || install -m 600 examples/factory.toml "$masf_config"
masf --config "$masf_config" doctor --offline
```

A partir de I2, construye la imagen del runner según el README de la versión instalada. Configura GitHub en el servicio y autentica las CLIs seleccionadas en el entorno en que se ejecutarán. El modo de acceso y las cuotas dependen de las cuentas del operador. `doctor` debe informar de binarios, versiones y dependencias comprobables sin iniciar una tarea de pago.

## 2. Dar de alta el destino

1. Elige un repo propio de pruebas con una rama predeterminada y anota el SHA inicial. Como caso mínimo, usa `saludo.py` con una función `saludo(nombre)` que devuelva `f"Hola, {nombre}"` y una prueba existente en `tests/test_saludo.py` para la entrada `"Ana"`.
2. Verifica sobre esa base que `python3 -m unittest discover -s tests` ejecuta al menos una prueba y pasa. Es el comando del perfil de ejemplo.
3. En `pilot.toml` sustituye `example-org/masf-pilot` por el destino real y activa `enabled` de esa entrada. Elige una etiqueta existente o crea `masf:ready` en GitHub. El alta solo requiere esa configuración y los permisos del repositorio.
4. En I2 asigna `plan`, `build` y `review` a `codex`. Desde I3 puedes usar Codex para planificar e implementar y Cursor para revisar, tal como indica el ejemplo. Para una ejecución con los tres, habilita Claude Code y asigna `plan = "claude"` a ese repositorio. Guarda las credenciales fuera del TOML.
5. Comprueba `masf --config "$masf_config" doctor` cuando esté disponible. Anota qué proveedores se usarán. Un proveedor pendiente de credenciales puede quedar deshabilitado.
6. Comprueba que la identidad que publicará tiene acceso al destino elegido. Registra por separado la lectura de issues, la escritura de ramas y la creación de PR. En una instalación nueva, la primera publicación real confirma las dos últimas operaciones.

## 3. Ejecutar una tarea observable

Crea una issue en el repositorio de pruebas con el siguiente encargo.

> Ajusta `saludo(nombre)` para quitar espacios de los extremos. Con `"  Ana  "` debe devolver `"Hola, Ana"`, con `"   "` debe devolver `"Hola, mundo"` y con `"Eva"` debe seguir devolviendo `"Hola, Eva"`. Añade pruebas para esos comportamientos.

Ejecuta `scan --dry-run` antes y después de añadir la etiqueta. La issue debe aparecer únicamente después de etiquetarla. Con dos ejecuciones de `scan` debe figurar una sola vez en `status`. Desde I2, `run --once` ejecuta el primer trabajo manual.

```bash
masf --config "$masf_config" scan --dry-run
masf --config "$masf_config" scan
masf --config "$masf_config" scan
masf --config "$masf_config" status
masf --config "$masf_config" run --once
masf --config "$masf_config" status
```

Abre el PR en borrador. Comprueba los tres ejemplos en el SHA publicado, revisa el diff, verifica qué proveedor hizo cada etapa y contrasta el resultado de los checks con el del PR. La rama base debe conservar su SHA en este repo de prueba hasta que una persona decida fusionar. El diff debe contener únicamente los cambios propios de la tarea.

Desde I4 inicia `masf --config "$masf_config" serve` y etiqueta una **segunda issue** independiente, por ejemplo añadir al README del proyecto un ejemplo de uso de `saludo("Eva")` con su salida. El servicio ha de recogerla sin una orden manual. Reinícialo y comprueba que recupera el estado. Los casos de caída en el punto exacto de publicación también se reproducen mediante la prueba controlada de I2.

## 4. Decidir si ampliar el uso

| Evidencia | Criterio para el piloto |
| --- | --- |
| Descubrimiento y recuperación | Dos sondeos y un reinicio dejan el mismo id de trabajo y un único PR por issue. |
| Alcance del cambio | Los tres comportamientos se verifican en el SHA del PR. El repo destino conserva su estructura y la decisión de merge. |
| Intercambio de proveedores | El plan y el diff pasan entre al menos dos CLIs reales. El registro identifica por separado cualquier adaptador que solo se ejercitó con un doble de prueba. |
| Fallos | Un check fallido impide la publicación y deja evidencia. El número de rondas se mantiene dentro del límite. Un fallo de autenticación indica qué proveedor necesita atención. |
| Alta de un segundo proyecto | Una segunda entrada en el TOML basta para descubrir su issue. Un número de issue coincidente en ambos repos produce trabajos distintos. |
| Coste del proceso | Quedan anotados tiempo por etapa, rondas, intervenciones y uso disponible. Si no hay datos de coste, el informe lo indica. |

Conserva en la instalación del operador la revisión de MASF, versiones de las CLIs ejecutadas, id del trabajo, SHA, URL del PR y el resultado de cada criterio. Esos datos permiten decidir qué proveedor asignar a cada rol y qué fallo corregir antes de incorporar proyectos reales. Al terminar, detén `serve` y deshabilita las entradas de prueba. El historial y los PR permiten revisar lo ocurrido después.
