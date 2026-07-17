# jira-gateway

MCP local que expone 3 acciones al agente: `list_my_tasks`, `create_task`
(con confirmación obligatoria en dos pasos) y `start_task`. Alcance
deliberadamente reducido a Jira — git (rama, commits, push, historial) lo
sigue manejando Claude Code directamente por bash, como ya hacías. Este
gateway no intenta ser una barrera para git; solo cubre lo que el agente no
puede hacer por sí mismo: hablar con Jira sin ver tus credenciales.

## Instalación

Requiere Python >=3.11. Si tu Python de sistema es más antiguo, usa
[`uv`](https://docs.astral.sh/uv/) para que te instale un 3.11 aislado en el
propio `.venv` sin tocar nada del sistema:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd jira-git-gateway
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .
```

(Alternativa sin `uv`, si ya tienes Python 3.11+ disponible en el sistema:
`python3 -m venv .venv && source .venv/bin/activate && pip install -e .`)

## Configuración (variables de entorno)

Crea `~/.jira-gateway.env` (fuera de este repo — la ruta exacta es
configurable con `JIRA_GATEWAY_ENV_FILE` si quieres otra):

```bash
JIRA_EMAIL=tu-email@dominio.com
JIRA_API_TOKEN=el-token-con-scope-write:jira-work-y-read:jira-work
JIRA_CLOUD_ID=...           # GET https://tudominio.atlassian.net/_edge/tenant_info
JIRA_SITE_URL=https://tudominio.atlassian.net
JIRA_PROJECT_KEY=PROJ
JIRA_IN_PROGRESS_STATUS=In Progress  # opcional, ajusta al nombre real de tu workflow
JIRA_SELECTED_STATUS=Selected for Development  # opcional, estado al que pasa create_task tras crear
JIRA_DEFAULT_ISSUE_TYPE=Task  # opcional, ajusta al nombre real de tu tipo de issue
JIRA_SUBTASK_ISSUE_TYPE=Subtask  # opcional, tipo usado al crear con parent_key sin issue_type explícito
```

`gateway/config.py` lo carga solo (vía `python-dotenv`) al arrancar —no hace
falta exportarlo en tu shell ni pasarlo por la config de MCP. Motivo de que
viva fuera del repo: así no está a la vista dentro del directorio que Claude
Code tiene abierto mientras curras. No es una barrera de seguridad dura —un
agente con Bash sin restricciones podría igualmente leer esa ruta si se lo
propone— pero evita la exposición accidental y evita duplicar el token en
`~/.claude.json` al configurar el MCP. Si quieres una barrera más fuerte
(un usuario Unix separado que de verdad no pueda leer el token), usa el
modo servicio de la sección de abajo.

## Añadirlo a Claude Code

Hay dos formas de conectarlo. Ojo: en ambas, Claude Code corre con tu mismo
usuario del sistema, así que cualquier cosa que ese usuario pueda leer
(incluido un `.env` en el propio repo, o la config de MCP donde metas el
token) el agente también puede leerla por Bash si se lo propone — el
subproceso stdio no es una barrera real contra eso, solo una forma cómoda
de que el agente no necesite tocar el token para hacer su trabajo normal.

### Opción A — stdio (rápida, sin aislamiento real de credenciales)

Como `~/.jira-gateway.env` ya lo carga el propio `config.py`, aquí no hace
falta pasar ningún `env` — así el token tampoco queda duplicado dentro de
`~/.claude.json`:

```json
{
  "mcpServers": {
    "jira-gateway": {
      "command": "/ruta/a/jira-git-gateway/.venv/bin/python",
      "args": ["-m", "gateway.server"]
    }
  }
}
```

(No hace falta `cwd`: el paquete queda instalado en modo editable en el
venv, así que `-m gateway.server` funciona desde cualquier directorio.)

Vale para uso personal en el que confías en que el agente usa las tools
porque son el camino natural para lo que le pides, no porque no tenga forma
de saltárselas.

### Opción B — servicio systemd bajo usuario separado (aislamiento real)

`scripts/setup_service.sh` despliega el gateway bajo un usuario Unix
dedicado (`jira-gw`, sin login), con el `.env` en `/opt/jira-gateway/.env`
(modo `600`, propiedad de `jira-gw`) — tu usuario normal no puede leerlo ni
por `cat` ni por ninguna otra vía, porque no tiene permisos de sistema
sobre esos ficheros. El gateway corre como servicio (`streamable-http`) y
Claude Code se conecta por red, sin ver el token en ningún momento:

```bash
sudo bash scripts/setup_service.sh
```

Y en la config de Claude Code, sin credenciales:

```json
{
  "mcpServers": {
    "jira-gateway": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

Es más montaje (usuario de sistema, systemd, redeploy con el script cuando
cambies código), pero es la única de las dos opciones donde "el agente no
puede leer el token" es una garantía técnica y no solo una expectativa de
buen comportamiento.

## Flujo de uso

1. "¿Qué tareas tengo pendientes?" → `list_my_tasks`
2. "Crea una tarea para X" → `create_task` (sin `confirm`) → el agente te
   enseña el preview (proyecto, tipo, resumen, descripción, etiquetas) → si
   dices que sí, el agente vuelve a llamar a `create_task` con `confirm=True`
   y los mismos datos → ahí sí se crea. Acepta `labels` opcional (lista de
   strings) para etiquetar el issue al crearlo.
3. "Selecciona la PROJ-123 para desarrollo" → `start_task` con
   `status="Selected for Development"` (o el nombre exacto de la transición
   intermedia de tu workflow).
4. "Empieza la PROJ-123" → `start_task` sin `status` → transiciona al estado
   de "en progreso" configurado en `JIRA_IN_PROGRESS_STATUS`.
5. Claude Code crea la rama, desarrolla, comitea y hace push con sus
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
- **`start_task` solo transiciona issues asignados a ti**: comprueba el
  `assignee` contra el usuario del token antes de tocar nada. Si el issue es
  de otra persona (o está sin asignar), falla sin transicionar. Esto no
  aplica a la transición automática de `create_task` a `JIRA_SELECTED_STATUS`,
  ya que un issue recién creado normalmente está aún sin asignar.

## Notas

- No he podido instalar/testear `mcp` en el entorno donde escribí esto (sin
  red). Antes de usarlo en serio, pásalo por tu Claude Code local para que
  compile, corra `pip install -e .` y valide el import — probablemente haga
  falta algún ajuste menor de API si tu versión de `mcp` difiere.
- El scope de token recomendado: clásico `write:jira-work` + `read:jira-work`
  (los granulares de escritura tienen un bug conocido en POST a fecha de
  hoy — ver conversación anterior).
