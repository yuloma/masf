# Dashboard de la instalación central

Mantén el controlador habitual (`masf --config /ruta/privada/config.toml serve`). El dashboard comparte su registro, estado y evidencia. Ejecuta el servidor HTTP en otra terminal del mismo host y abre `http://127.0.0.1:8765/dashboard/`.

```bash
masf --config /ruta/privada/config.toml observe-api
```

Usa el directorio de estado de la instalación (`$XDG_STATE_HOME/masf`, por defecto `~/.local/state/masf`). El observador con montajes de solo lectura también sirve esta vista. `--port` cambia el puerto del HTTP. [API de observación y despliegue](observacion.md).

## Intervenir desde el navegador

En el host central, crea un archivo privado con la identidad del operador y un token aleatorio. Este ejemplo crea el archivo solo si su nombre está libre.

```bash
umask 077
python3 - /ruta/privada/dashboard-operator.json <<'PY'
import json
import secrets
import sys

token = secrets.token_hex(32)
with open(sys.argv[1], "x", encoding="utf-8") as auth:
    json.dump({"operator": "Operadora local", "token": token}, auth)
print(token)
PY
masf --config /ruta/privada/config.toml observe-api --port 8765 \
  --actions-token-file /ruta/privada/dashboard-operator.json
```

Ejecuta este modo con acceso de escritura al estado, lectura de las copias conservadas y las credenciales de GitHub de la instalación para revalidar la issue y el PR. Conserva los mismos valores de `XDG_STATE_HOME`, `XDG_DATA_HOME` y `MASF_JOBS_DIR` que el controlador. El puerto sirve únicamente en loopback. Reinicia el servicio HTTP después de cambiar el archivo de autenticación. El Compose del observador monta el estado en solo lectura.

En **Intervenciones**, abre el trabajo y escribe el token en **Consultar acciones**. La pestaña lo conserva en memoria mientras mantienes abierto el detalle. La bandeja distingue preguntas, cambios de alcance y bloqueos. El detalle muestra los SHAs y evidencia, el efecto previsto de cada acción y el recibo. El token identifica al operador indicado en el archivo.

| Acción | Efecto y comprobación |
| --- | --- |
| Responder pregunta | La planificación recibe la pregunta y la respuesta registradas. |
| Confirmar alcance | El operador revisa la issue y los extractos filtrados disponibles. El servidor confirma la huella del alcance observado y vuelve a planificar. |
| Autorizar rutas | El servidor comprueba el candidato, el alcance y los archivos exactos. El formulario presenta las rutas y el resumen de líneas modificadas. El operador confirma la lista antes de volver a checks. |
| Confirmar causa resuelta | El controlador vuelve a la etapa anunciada. Si el CI requería corregir el entorno o relanzar la ejecución, vuelve a observar el PR. |
| Reasignar proveedor | El servidor ofrece proveedores habilitados para el rol de la etapa que se reanudará. La asignación se registra para ese trabajo. |
| Ampliar rondas | El formulario calcula el nuevo límite. El servidor admite de 1 a 25 rondas adicionales en una decisión sobre un límite agotado y las vincula al alcance vigente. |
| Ampliar plazo y recuperar copia | El operador selecciona una copia de construcción compatible, actual o histórica, y confirma un plazo mayor al agotado y de hasta 86 400 segundos. La construcción continúa con su plan y archivos en una ronda ya contabilizada. |

El controlador recoge el trabajo en su siguiente despacho normal. El dashboard sondea cada 10 segundos mientras la pestaña está visible. **Actualizar** solicita una lectura inmediata. La cronología conserva las operaciones, la evidencia, los intentos y los resultados de CI para los SHAs anteriores. Tras un conflicto, revisa el nuevo contexto y vuelve a marcar la confirmación. Si el transporte falla, **Reintentar el mismo envío** recupera el recibo con la misma clave.

## Comprobar la instalación

El primer comando verifica las dos API mediante HTTP local, una cola temporal, repositorios Git temporales y fixtures de GitHub y proveedores. El segundo usa Playwright con Chromium ya instalado. Abre el dashboard servido por la misma API, envía decisiones desde el navegador y guarda capturas PNG.

```bash
python3 -m unittest tests.test_observation_api tests.test_dashboard -v
MASF_DASHBOARD_BROWSER=1 MASF_DASHBOARD_EVIDENCE=/tmp/masf-dashboard-evidence \
  python3 -m unittest tests.test_dashboard_browser -v
```

Las capturas numeradas cubren cola, etapa activa en móvil, trabajo terminado, error de autenticación, conflicto, respuesta, ruta protegida, checks, reanudación completa, timeout y reparación de CI. La prueba comprueba que las decisiones persisten antes de que el controlador despache el trabajo. [Aceptación de I5](implementacion.md#i5--dashboard-e-intervención).
