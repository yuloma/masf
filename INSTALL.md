# Instalar y poner en marcha MASF

Si recibes el prompt del README, tu encargo termina cuando una instalación central procesa una issue de GitHub con un agente real, abre un PR en borrador verificable y queda atendiendo la cola después de cerrar tu sesión. Ejecuta los pasos en la máquina que alojará el servicio. Los comandos de `masf` citados aquí son los que la revisión instalada debe proporcionar.

## 1. Localiza la fábrica y el destino de prueba

- Usa el checkout de `yuloma/masf` o la revisión que haya indicado el operador. Desde otra carpeta, clona `https://github.com/yuloma/masf.git` en un directorio dedicado. Anota la ruta y el resultado de `git rev-parse HEAD`. Usa el manual y el código de ese mismo commit. El código, los adaptadores y las mejoras de instalación pertenecen a MASF. Los proyectos atendidos se registran en la configuración central.
- Identifica el repositorio piloto autorizado, la etiqueta de admisión y los comandos de prueba de su rama base. Si falta el destino, pide un `OWNER/REPO` donde puedas crear o reutilizar una issue y abrir un PR en borrador. Puedes preparar y probar la fábrica mientras se concreta ese dato.
- Comprueba Docker Engine local y Compose en el host. Para el controlador nativo se necesitan además Python 3.11+, Git y `gh`. En Docker esas herramientas van en la imagen. Identifica las cuentas disponibles para los roles del piloto. Usa los mecanismos de autenticación del operador, con los secretos fuera del chat, del TOML y de Git. El servicio necesita leer issues, subir ramas y crear PR. El instalador necesita poder crear o etiquetar la issue si el operador no la ha preparado.

## 2. Instala lo que exista e implementa lo que falte

Lee [AGENTS.md](AGENTS.md), la [especificación](docs/especificacion.md) y las [aceptaciones de I1 a I4](docs/implementacion.md). Inspecciona el paquete, las imágenes, los adaptadores y los comandos realmente disponibles en esta revisión. Si ya cubren el flujo completo, usa su procedimiento de instalación y pasa al despliegue. Si faltan capacidades, impleméntalas en una rama de MASF, prueba cada incremento y continúa con el siguiente dentro del mismo encargo.

| Incremento | Qué tiene que funcionar |
| --- | --- |
| I1 | Instalar el paquete y construir la imagen del controlador. `doctor --offline` valida el registro. `scan --dry-run` muestra las issues elegibles, `scan` las registra una sola vez en SQLite y `status` permite consultarlas. |
| I2 | `run --once` lleva una tarea desde la cola hasta un PR en borrador. Plan, implementación, checks y revisión trabajan sobre el código y el SHA correspondientes. `resume` recupera un trabajo tras corregir la causa del bloqueo. Las pruebas de proyecto funcionan en imagen individual y en Compose. |
| I3 | Codex, Cursor y Claude Code implementan el mismo contrato. Configurar `plan`, `build` y `review` permite mezclar proveedores o usar uno solo. Cada adaptador tiene pruebas de contrato y cada CLI que se use en el piloto debe funcionar en el runner. |
| I4 | `serve` descubre y procesa issues periódicamente. Reiniciar el controlador conserva el estado, el trabajo y el PR. El servicio funciona con el editor y la terminal cerrados. |

Si has añadido capacidades, ejecuta las pruebas de aceptación del incremento con GitHub y los agentes simulados, y las de aislamiento con Docker real. Conserva los commits en MASF y documenta los comandos de construcción e instalación que hayas usado. Con acceso de escritura, abre o actualiza el PR de implementación. Puedes instalar desde el SHA probado de esa rama mientras se revisa.

## 3. Despliega una instancia central

1. Despliega con Docker Compose por defecto. Construye las imágenes del controlador y del runner de la revisión probada. Monta la configuración, el estado y los trabajos según la [guía de Docker](docs/docker.md#desplegar-el-controlador). El socket solo llega al controlador. Si el operador elige el controlador nativo, instala el paquete en un entorno virtual y configura `masf --config FILE serve` bajo el supervisor del host, con la ruta real del TOML. Ambas modalidades usan Docker para aislar las tareas.
2. En una instalación nueva, parte del [TOML de ejemplo](examples/factory.toml). Ajusta el slug, la etiqueta, `setup` y `checks` del piloto. Pon `enabled = true` en su entrada. Para un proyecto multiservicio, configura `files` y `service` en `[repositories.compose]` con un perfil de confianza en la instalación central. La aplicación y sus dependencias arrancarán con recursos distintos para la base y el candidato. Conserva los registros, los perfiles privados y SQLite al repetir el prompt.
3. Respeta las asignaciones del operador. En una instalación nueva con Codex y Cursor disponibles, usa Codex para planificar e implementar y Cursor para revisar. Con un solo proveedor autorizado, asígnale los tres roles. Claude Code se puede elegir para cualquiera de ellos. Registra qué combinaciones se ejercitaron con cuentas reales.
4. Configura el acceso a GitHub en el controlador y la autenticación de cada CLI en el runner que la ejecutará. Deben seguir disponibles al reiniciar el servicio. Ejecuta `doctor --offline`, `doctor` y `scan --dry-run` con la configuración efectiva. Comprueba que los checks del piloto pasan en la base antes de usar los agentes. Arranca una sola instancia de `serve` con reinicio configurado. Si ya está en marcha, reutiliza ese servicio y su estado.

En Docker, prepara las variables y el array Bash `control` de la guía, incluida la extensión del motor. Desde el checkout de MASF puedes arrancar y consultar el servicio con estos comandos.

```bash
"${control[@]}" up -d controller
"${control[@]}" ps controller
"${control[@]}" exec -T controller masf --config /etc/masf/pilot.toml status
"${control[@]}" logs --tail 40 controller
```

## 4. Acredita la instalación con una issue real

- Elige o crea en el piloto una issue pequeña con un comportamiento comprobable. Puedes usar el [ejercicio de saludo](docs/piloto.md#3-ejecutar-una-tarea-observable) en un repo desechable preparado para él. Aplica la etiqueta configurada y deja que **el servicio** descubra la issue, ejecute los agentes y publique. En una reanudación, busca primero la issue, su trabajo y su PR para continuar desde el último paso confirmado.
- Sigue `status` hasta `pr_open`. Si el trabajo se bloquea, consulta la causa, corrígela y reanuda la misma issue con `resume OWNER/REPO#N`. Contrasta en GitHub el destino, la issue enlazada, el estado de borrador y el SHA del PR con el SHA de los checks y de la revisión. Revisa que el diff cumpla el criterio de la issue. La decisión de fusionar corresponde al operador.
- En una instalación nueva, recrea el controlador en reposo conservando su estado o reinicia su servicio nativo. Tras el siguiente sondeo, verifica que sigue activo, que `status` conserva el mismo trabajo y que continúa habiendo un único PR para esa issue. Al repetir el prompt, si el servicio sigue activo con la misma revisión, perfil y asignación de proveedores, contrasta la evidencia guardada antes de lanzar otra tarea.
- Entrega la revisión instalada, el modo de despliegue, dónde editar la lista de repositorios, los comandos exactos para consultar estado y logs, los enlaces a la issue y al PR, el SHA y los proveedores y checks observados. Deja el servicio en marcha. Si una dependencia externa impide la prueba, completa lo que puedas verificar y comunica el bloqueo concreto y cómo reanudar con la misma instalación. Reserva el resultado **operativa** para una prueba real completada en el host del operador.
