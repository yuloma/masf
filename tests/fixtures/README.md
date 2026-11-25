# Procedencia de las muestras de I3

| Archivo | Origen | Versión y rol | Comando y formato |
| --- | --- | --- | --- |
| [cursor-2026.11.11-b912975.json](cursor-2026.11.11-b912975.json) | Captura real incorporada en el PR #6 de `yuloma/masf`. El archivo conserva la envoltura JSON y el progreso concatenado en `result`. | Cursor Agent `2026.11.11-b912975`, `plan`. | `agent -p --output-format json` es el modo de captura documentado. El registro local carece del comando completo con sus argumentos y prompt originales. [Interfaz documentada](../../docs/especificacion.md#proveedores-y-contrato-de-etapa). |
| [crash_controller.py](crash_controller.py) | Harness sintético de recuperación del controlador con un repositorio temporal, GitHub simulado en localhost y un proveedor de prueba. | Roles `plan`, `build` y `review`, sin CLI de proveedor. | `python3 -m unittest tests.test_process_recovery -v`. El harness genera sus respuestas durante la prueba. |

La captura de Cursor sustituye `session_id` y `request_id` por `fixture`. Conserva el tipo de resultado, tiempos, contadores de uso y texto relativo al README de MASF. Se revisaron sus campos y el código del harness para excluir credenciales, identificadores personales y contenido privado de otros proyectos.

La matriz de [contratos](../test_operation.py) genera en código muestras **sintéticas**, sin versión de CLI real. Incluye Codex JSONL, Cursor JSON y stream-json, y Claude JSON con `result` y con `structured_output`, en los tres roles.

## Disponibilidad de otras capturas reales

- **Codex.** El piloto documenta una ejecución de `0.167.1` en el rol `build`. Su salida original queda pendiente de aportación por el operador. [Registro del piloto](../../docs/piloto.md#intervención-operativa-del-25-de-noviembre-de-2026).
- **Cursor.** El piloto documenta revisiones de `2026.11.22-e9042b2`. La salida original de esas revisiones queda pendiente de aportación por el operador. [Registro del piloto](../../docs/piloto.md#evidencia-de-recuperación-autónoma-del-24-de-noviembre-de-2026).
- **Claude.** No hay una captura real disponible localmente. La evidencia versionada para Claude es sintética y la captura real es optativa. [Protocolo del piloto](../../docs/piloto.md#capturas-optativas-de-proveedores).
