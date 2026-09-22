# jira-gateway

MCP local que expone acciones al agente: `list_my_tasks`,
`list_unassigned_tasks`, `list_task_comments`, `create_task` y `add_comment`
(ambas con confirmación obligatoria en dos pasos), y `start_task` para
Jira; y, si Bitbucket está configurado, `create_pull_request` (también con
confirmación en dos pasos), `list_open_pull_requests` y `get_pull_request`
para pull requests de Bitbucket Cloud. Alcance deliberadamente acotado a
Jira y pull requests de Bitbucket — git (rama, commits, push, historial) lo
sigue manejando Claude Code directamente por bash, como ya hacías. Este
gateway no intenta ser una barrera para git; solo cubre lo que el agente no
puede hacer por sí mismo: hablar con Jira/Bitbucket sin ver tus
credenciales.

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

Crea `~/.secrets/jira-gateway.env` (fuera de este repo — la ruta exacta es
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

Bitbucket es **opcional**: si no añades estas variables, el gateway arranca
igual solo con Jira, y `create_pull_request` / `list_open_pull_requests` /
`get_pull_request` devuelven un error explicando qué falta. Ver la sección
[Bitbucket Cloud (pull requests)](#bitbucket-cloud-pull-requests) más abajo
para cómo sacar el token.

```bash
BITBUCKET_EMAIL=tu-email@dominio.com
BITBUCKET_API_TOKEN=el-token-con-scopes-read:repository:bitbucket-read:pullrequest:bitbucket-write:pullrequest:bitbucket
BITBUCKET_WORKSPACE=tu-workspace
BITBUCKET_ALLOWED_REPOS=repo-uno,repo-dos  # allowlist dura, sin espacios; vacío = las tools de Bitbucket fallan siempre
BITBUCKET_DEFAULT_REPO=repo-uno  # opcional, repo usado si la tool no recibe repo_slug
BITBUCKET_DEFAULT_TARGET_BRANCH=pre  # opcional, rama destino si la tool no recibe target_branch
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

Como `~/.secrets/jira-gateway.env` ya lo carga el propio `config.py`, aquí
no hace falta pasar ningún `env` — así el token tampoco queda duplicado
dentro de `~/.claude.json`:

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

`scripts/setup_service.sh <empresa>` despliega el gateway bajo un usuario
Unix dedicado (`jira-gw-<empresa>`, sin login), con el `.env` en
`/opt/jira-gateway-<empresa>/.env` (modo `600`, propiedad de ese usuario) —
tu usuario normal no puede leerlo ni por `cat` ni por ninguna otra vía,
porque no tiene permisos de sistema sobre esos ficheros. El gateway corre
como servicio (`streamable-http`) y Claude Code se conecta por red, sin ver
el token en ningún momento.

Se despliega con un solo comando:

```bash
sudo bash scripts/setup_service.sh acme
```

Crea el usuario `jira-gw-acme`, `/opt/jira-gateway-acme`, y si el `.env` de
ahí dentro no existe todavía, lo crea vacío (permisos `600`, propiedad de
`jira-gw-acme`) con las mismas claves que la sección
[Configuración](#configuración-variables-de-entorno) de arriba. En la misma
pasada instala dependencias y crea la unidad systemd. Al final te avisa si
quedan credenciales por rellenar; edítalas con:

```bash
sudo -u jira-gw-acme nano /opt/jira-gateway-acme/.env
```

El servicio no arrancará hasta que estén todas rellenas (`config.py` falla
con un error claro si falta alguna).

Soporta varias empresas en la misma máquina (una unidad systemd por
empresa), pero **todas escuchan en el mismo puerto fijo** (`8765` por
defecto) y **nunca hay dos activas a la vez** — un puerto local sin
autenticación es alcanzable por cualquier proceso de la máquina, así que
tener dos empresas escuchando en paralelo abriría la puerta a que un agente
trabajando en el proyecto de una empresa hablara con el gateway de otra.
Para alternar entre empresas:

```bash
sudo bash scripts/jira-switch.sh acme       # para el resto, activa "acme"
bash scripts/jira-switch.sh                 # lista qué instancia está activa (no necesita sudo)
```

Como el puerto nunca cambia, la config de Claude Code tampoco — es la misma
entrada pase lo que pase con qué empresa esté activa:

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
cambies código, acordarte de `jira-switch` al cambiar de cliente), pero es
la única opción donde "el agente no puede leer el token, ni el de esta
empresa ni el de otra" es una garantía técnica y no solo una expectativa de
buen comportamiento.

Ambos scripts aceptan `--help`/`-h` si necesitas recordar el uso sin abrir
este README:

```bash
bash scripts/setup_service.sh --help
bash scripts/jira-switch.sh --help
```

### Crear una instalación nueva

```bash
sudo bash scripts/setup_service.sh <empresa>
sudo -u jira-gw-<empresa> nano /opt/jira-gateway-<empresa>/.env   # rellena credenciales
sudo bash scripts/jira-switch.sh <empresa>                        # la activa (para el resto)
```

### Actualizar una instalación existente (tras cambiar código del repo)

`setup_service.sh` es idempotente: re-publica el código (`rsync`), reinstala
dependencias y regenera la unidad systemd, pero **no reinicia el servicio a
propósito** (para no reiniciar en caliente una instancia que no es la que
estás tocando). Para que el proceso ya corriendo recoja el código nuevo:

```bash
sudo bash scripts/setup_service.sh <empresa>
sudo systemctl restart jira-gateway-<empresa>.service
```

Solo hace falta el `restart` si `<empresa>` es la instancia activa ahora
mismo (compruébalo con `bash scripts/jira-switch.sh`, sin sudo); si no está
activa, el redeploy ya deja el código listo para la próxima vez que la
actives con `jira-switch.sh`.

### Añadir Bitbucket a una instalación ya desplegada

`setup_service.sh` solo crea el `.env` si no existe todavía — si tu
instancia ya está desplegada (solo con Jira), las variables `BITBUCKET_*`
no aparecen solas. Añádelas a mano al `.env` de esa instancia:

```bash
sudoedit /opt/jira-gateway-<empresa>/.env
# (alternativa equivalente: sudo -u jira-gw-<empresa> nano /opt/jira-gateway-<empresa>/.env)
```

Pega el bloque `BITBUCKET_*` de la sección
[Configuración](#configuración-variables-de-entorno), rellena los valores
reales y reinicia el servicio para que los recoja:

```bash
sudo systemctl restart jira-gateway-<empresa>.service
```

## Bitbucket Cloud (pull requests)

Las app passwords de Bitbucket están retiradas (fin de soporte:
28-jul-2026). La autenticación usa Basic auth con tu email de Atlassian +
un **API token de cuenta** (no un app password) contra
`https://api.bitbucket.org/2.0`. Créalo en
[id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens)
con exactamente estos scopes:

- `read:repository:bitbucket`
- `read:pullrequest:bitbucket`
- `write:pullrequest:bitbucket`

**Ese token lo generas y lo pegas en el `.env` del servicio tú mismo — el
agente nunca lo pide ni lo escribe.**

Tools disponibles (nada de merge, approve, decline ni push — eso lo sigues
haciendo tú o Bitbucket en el momento del merge):

- `create_pull_request(source_branch, title, description, repo_slug=None, target_branch=None, close_source_branch=True, confirm=False)` —
  mismo protocolo preview→confirm que `create_task`: sin `confirm` devuelve
  exactamente lo que se enviaría (repo, rama origen, rama destino, título,
  descripción completa, `close_source_branch`) sin tocar Bitbucket; con
  `confirm=True`, si ya hay un PR abierto para esa rama origen no crea uno
  nuevo (devuelve el existente con `already_exists: true`), si no lo crea.
  La descripción la redacta el agente (a partir de los commits de la rama y
  la clave Jira que suele ir en el nombre, ej. `feature/REF-432-...`); el
  gateway no la inventa, solo la transporta. No se envían reviewers
  explícitos: si el repo tiene default reviewers configurados en Bitbucket,
  se añaden solos al crear el PR.
- `list_open_pull_requests(repo_slug=None)` — solo lectura, PRs abiertos del
  repo (por defecto `BITBUCKET_DEFAULT_REPO`).
- `get_pull_request(pr_id, repo_slug=None)` — solo lectura, detalle de un PR
  por id.

`repo_slug` está siempre restringido a la allowlist `BITBUCKET_ALLOWED_REPOS`
del `.env` — un repo fuera de esa lista falla sin tocar Bitbucket, y una
allowlist vacía hace fallar las tools siempre (nunca "todos los repos del
workspace").

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
5. "¿Qué comentarios tiene la PROJ-123?" → `list_task_comments` (solo
   lectura, autor/fecha/texto en orden cronológico).
6. "Comenta en la PROJ-123 que..." → `add_comment` (sin `confirm`) → el
   agente te enseña el preview del texto → si dices que sí, vuelve a llamar
   con `confirm=True` para publicarlo.
7. Claude Code crea la rama, desarrolla, comitea y hace push con sus
   herramientas normales de bash/git — el gateway no interviene en nada de
   esto, y puede seguir leyendo `git log`/`git diff`/`git blame` sin
   restricción alguna.
8. Si Bitbucket está configurado: "Abre el PR de esta rama" →
   `create_pull_request` (sin `confirm`) → el agente te enseña el preview
   (repo, rama origen/destino, título, descripción completa) → si dices que
   sí, vuelve a llamar con `confirm=True` → lo crea, o te devuelve el que ya
   estuviera abierto para esa rama si lo había. Si no está configurado,
   abres el PR a mano como hasta ahora.
9. "¿Qué PRs hay abiertos en refunder-react?" → `list_open_pull_requests`.
   "¿Cómo va el PR 87?" → `get_pull_request`. Ambas de solo lectura.

Nota sobre la confirmación: además del preview de `create_task`, Claude Code
ya te pide aprobación antes de ejecutar cualquier llamada a un MCP no
auto-aprobado (verás el JSON de parámetros antes de que se dispare). El
preview de `create_task` es una capa extra pensada para que la revisión sea
legible (texto formateado) en vez de JSON crudo — como cuando revisas el
mensaje de un commit antes de confirmarlo.

## Por qué está diseñado así

- **Catálogo cerrado de tools**: solo lo que aparece arriba, nada toca git
  ni ejecuta comandos arbitrarios. No hay "ejecuta este comando" genérico,
  ni JQL libre en Jira, ni query BBQL libre en Bitbucket.
- **Bitbucket con allowlist dura de repos** (`BITBUCKET_ALLOWED_REPOS`):
  el token puede tener acceso a más repos del workspace de los que quieres
  que el agente toque; la allowlist es la barrera, no el scope del token.
- **`create_pull_request` no duplica PRs**: antes de crear, busca si ya hay
  uno abierto para esa rama origen y devuelve ese en vez de crear otro.
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
