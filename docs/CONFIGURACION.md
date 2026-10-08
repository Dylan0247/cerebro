# Configuración de la copia pública

El código procede de la implementación desarrollada y se ha adaptado para retirar rutas personales. Los workflows son plantillas de exportaciones de desarrollo, desactivadas y sin credenciales ni identificadores de la instalación. La adaptación pública no ha sido desplegada de extremo a extremo en otro entorno.

## Requisitos

- Python 3.12 en Linux para las operaciones con descriptores, bloqueo y rechazo de enlaces simbólicos.
- n8n con los tipos de nodos incluidos en los workflows.
- Ollama con el modelo definido en `src/cerebro_model_config.py`.
- Una bóveda de prueba y un directorio privado de datos.

No ejecutar contra una bóveda personal antes de revisar la configuración y probar con datos sintéticos.

## Variables

Copiar `config.env.example` a un archivo privado y completar:

- `CEREBRO_VAULT`: ruta absoluta de la bóveda.
- `CEREBRO_DATA_DIR`: directorio privado para token, cola y propuestas.
- `CEREBRO_CODEX_SESSIONS`: ruta de registros compatibles; necesaria para el sincronizador y el MCP que lo importa.
- `CEREBRO_START_DATE`: fecha mínima de captura; por defecto se utiliza el día de ejecución.

Exportar las variables antes de iniciar los procesos. El archivo de ejemplo no se carga automáticamente. La cola puede contener extractos privados y no debe publicarse.

Crear en el directorio privado un archivo `token` con una credencial aleatoria propia de al menos 32 caracteres, accesible únicamente por el usuario del servicio. No reutilizar credenciales del entorno original ni incluirlas en Git.

## Servicios y endpoints de ejemplo

Los endpoints loopback incluidos en el código son valores genéricos de referencia; no identifican un equipo ni son direcciones públicas:

- Lectura: `service.py`, puerto 18767.
- Preparación de relevos: `cerebro_relevo_http.py`, puerto 18768.
- n8n: puerto 5678.
- Ollama: puerto 11434.

Los componentes deben poder comunicarse por estas direcciones o adaptarse juntos. No exponer estos servicios directamente a Internet.

`deploy/` incluye lanzadores genéricos y una plantilla de unidad systemd. Deben adaptarse a las rutas elegidas. Los nombres de unidades usados en el lanzador Linux requieren crear las unidades correspondientes; la plantilla no instala automáticamente n8n ni Ollama. Las variables deben llegar tanto al lanzador como a cada servicio. `Start-Cerebro.ps1` utiliza el usuario predeterminado de la distribución y recibe la ruta Linux del lanzador como parámetro.

## Importación de workflows

1. Importar primero `workflows/lectura-obsidian.json`.
2. Importar `workflows/agente-contexto.json` y seleccionar el workflow de lectura en el nodo `buscar_obsidian`. `${READ_WORKFLOW_ID}` es un marcador que debe sustituirse desde la interfaz, no una variable interpolada automáticamente.
3. En el workflow de lectura, configurar la lista de workflows autorizados con el ID nuevo del agente; `${AGENT_WORKFLOW_ID}` también es un marcador.
4. Importar las plantillas de consulta, resumen y propuesta de relevo.
5. Asignar credenciales propias en cada webhook y petición autenticada: cabecera `Authorization` con valor `Bearer` seguido de la credencial propia. Configurar además la conexión de Ollama del agente.
6. Mantener la autenticación activada; comprobar los nodos y sus tipos en la versión instalada de n8n antes de publicar los workflows.

Se retiró el antiguo Code Tool deshabilitado y se añadió autenticación al webhook de la plantilla del agente. Estas adaptaciones afectan únicamente a la copia pública.

## Notas esperadas

La implementación usa los nombres `Cerebro_IA_Inicio`, `Arquitectura_Cerebro_IA`, `Estado_Global` y `Registro_Decisiones`. Las consolidaciones tienen destinos fijos bajo `01_Global`; los relevos y borradores se guardan bajo `05_Sesiones`. Crear las notas con contenido sintético y cabeceras adecuadas antes de probar.

## Validación y mantenimiento

El repositorio incluye comprobaciones estructurales y de privacidad en `tests/test_public_bundle.py`. No equivalen a las suites históricas ni a una prueba de integración con n8n y Ollama.

```bash
python3 -m unittest discover -s tests -v
```

Revisar el adaptador de registros cuando cambie su formato. La redacción de secretos del sincronizador es básica y no garantiza eliminar cualquier dato sensible de un resumen.

