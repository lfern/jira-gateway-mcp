# jira-gateway

MCP local que expone 3 acciones al agente: `list_my_tasks`, `create_task`
(con confirmación obligatoria en dos pasos) y `start_task`. Alcance
deliberadamente reducido a Jira — git (rama, commits, push, historial) lo
sigue manejando Claude Code directamente por bash, como ya hacías. Este
gateway no intenta ser una barrera para git; solo cubre lo que el agente no
puede hacer por sí mismo: hablar con Jira sin ver tus credenciales.

## Instalación

```bash
cd jira-git-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Configuración (variables de entorno)

```bash
export JIRA_EMAIL="tu-email@dominio.com"
export JIRA_API_TOKEN="el-token-con-scope-write:jira-work-y-read:jira-work"
export JIRA_CLOUD_ID="..."           # GET https://tudominio.atlassian.net/_edge/tenant_info
export JIRA_SITE_URL="https://tudominio.atlassian.net"
export JIRA_PROJECT_KEY="PROJ"
export JIRA_IN_PROGRESS_STATUS="In Progress"  # opcional, ajusta al nombre real de tu workflow
```

Guarda esto en un `.env` fuera del repo o en tu gestor de secretos habitual —
nunca en el propio proyecto ni en algo que Claude Code pueda leer o commitear.

## Añadirlo a Claude Code

En la config de MCP servers de Claude Code (`claude mcp add` o el JSON de
config), como servidor local por stdio:

```json
{
  "mcpServers": {
    "jira-gateway": {
      "command": "/ruta/a/jira-git-gateway/.venv/bin/python",
      "args": ["-m", "gateway.server"],
      "cwd": "/ruta/a/jira-git-gateway",
      "env": {
        "JIRA_EMAIL": "...",
        "JIRA_API_TOKEN": "...",
        "JIRA_CLOUD_ID": "...",
        "JIRA_SITE_URL": "...",
        "JIRA_PROJECT_KEY": "PROJ"
      }
    }
  }
}
```

Mejor pasar el `env` ahí que exportarlo en tu shell interactiva, para que solo
lo vea este proceso.

## Flujo de uso

1. "¿Qué tareas tengo pendientes?" → `list_my_tasks`
2. "Crea una tarea para X" → `create_task` (sin `confirm`) → el agente te
   enseña el preview (proyecto, tipo, resumen, descripción) → si dices que
   sí, el agente vuelve a llamar a `create_task` con `confirm=True` y los
   mismos datos → ahí sí se crea.
3. "Empieza la PROJ-123" → `start_task` (solo transiciona el issue en Jira)
4. Claude Code crea la rama, desarrolla, comitea y hace push con sus
   herramientas normales de bash/git — el gateway no interviene en nada de
   esto, y puede seguir leyendo `git log`/`git diff`/`git blame` sin
   restricción alguna.
5. Tú abres el PR a mano cuando toque.

Nota sobre la confirmación: además del preview de `create_task`, Claude Code
ya te pide aprobación antes de ejecutar cualquier llamada a un MCP no
auto-aprobado (verás el JSON de parámetros antes de que se dispare). El
preview de `create_task` es una capa extra pensada para que la revisión sea
legible (texto formateado) en vez de JSON crudo — como cuando revisas el
mensaje de un commit antes de confirmarlo.

## Por qué está diseñado así

- **Catálogo cerrado de tools**: solo 2 acciones, ambas de Jira, ninguna
  toca git. No hay "ejecuta este comando" genérico ni JQL libre.
- **Git queda fuera a propósito**: ya confías en Claude Code para manejar
  git por bash (commits, push, y también lectura de historial cuando lo
  necesitas), así que el gateway no intenta duplicar ni restringir eso —
  solo cubre lo que el agente no puede hacer solo, que es hablar con Jira
  sin ver tu token.
- **Validación de issue_key** (`PROJ-123`) antes de tocar Jira.
- **`create_task` nunca escribe en la primera llamada**: el flag `confirm`
  empieza en `False` por defecto, así que la ruta "segura" (preview) es la
  que sale sin que nadie tenga que acordarse de pedirla explícitamente.

## Notas

- No he podido instalar/testear `mcp` en el entorno donde escribí esto (sin
  red). Antes de usarlo en serio, pásalo por tu Claude Code local para que
  compile, corra `pip install -e .` y valide el import — probablemente haga
  falta algún ajuste menor de API si tu versión de `mcp` difiere.
- El scope de token recomendado: clásico `write:jira-work` + `read:jira-work`
  (los granulares de escritura tienen un bug conocido en POST a fecha de
  hoy — ver conversación anterior).
