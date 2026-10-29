# jira-gateway

MCP local que expone acciones al agente: `list_my_tasks`,
`list_unassigned_tasks`, `get_task`, `list_task_comments`, `create_task`,
`update_task` y `add_comment` (las tres con confirmación obligatoria en dos
pasos), `assign_task` (solo a ti y solo si está sin asignar), `start_task`,
`transition_task`, y `list_worklogs`, `add_worklog`, `update_worklog` y
`delete_worklog` para el tiempo dedicado (las de escritura con confirmación
en dos pasos; corregir y borrar, solo tus entradas), y
`list_pending_attachments` y `add_attachments` para adjuntar ficheros
desde una bandeja (confirmación en dos pasos) para Jira; y, si Bitbucket está configurado, `create_pull_request` (también con
confirmación en dos pasos), `list_open_pull_requests` y `get_pull_request`
para pull requests, más `run_pipeline` (confirmación en dos pasos, allowlist
dura de patrones) y `get_pipeline` para lanzar pipelines custom de
Bitbucket Cloud. Alcance deliberadamente acotado a Jira y Bitbucket (PRs y
pipelines custom) — git (rama, commits, push, historial) lo sigue
manejando Claude Code directamente por bash, como ya hacías. Este gateway
no intenta ser una barrera para git; solo cubre lo que el agente no puede
hacer por sí mismo: hablar con Jira/Bitbucket sin ver tus credenciales.

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
JIRA_ALLOWED_STATUSES=Listo para validar  # opcional, allowlist de estados destino (separados por comas); vacío = cualquiera
```

`JIRA_ALLOWED_STATUSES` limita a qué estados pueden mover una tarea
`start_task` y `transition_task`. Los estados de `JIRA_IN_PROGRESS_STATUS`
y `JIRA_SELECTED_STATUS` se permiten siempre. Si no la defines, se permite
cualquier transición del workflow, como hasta ahora.

### Adjuntos (opcional)

```bash
JIRA_ATTACHMENTS_DIR=/srv/jira-gateway-inbox-acme  # bandeja: única carpeta de la que se suben ficheros; vacío = adjuntos desactivados
JIRA_ATTACHMENTS_MAX_MB=10  # opcional, tamaño máximo por fichero
WSL_DISTRO_NAME=Ubuntu-22.04  # opcional, en WSL: el gateway arma con ella la ruta de Windows de la bandeja
```

El preview da la ruta de Windows de cada fichero
(`\\wsl.localhost\<distro>\srv\...`) para abrirlo con un clic. En modo
stdio, `WSL_DISTRO_NAME` ya viene en el entorno; en modo servicio la pone
`setup_service.sh`. Si necesitas otra ruta, existe
`JIRA_ATTACHMENTS_WINDOWS_DIR`, pero en el `.env` del servicio va **entre
comillas simples** (`'\\servidor\carpeta'`): sin comillas, systemd trata
las barras invertidas como escapes y se las come.

`add_attachments` **nunca acepta rutas**, solo nombres de fichero dentro de
la bandeja. Si aceptara rutas, en modo servicio el gateway podría subir a
Jira ficheros que tú no puedes leer, empezando por su propio `.env` con los
tokens. El agente copia el fichero a la bandeja con `install -m 644` y
pasa el nombre.
Se rechazan nombres con barras o punto inicial, enlaces simbólicos, hard
links, ficheros que no sean normales y tipos fuera de la lista (png, jpg,
gif, webp, pdf, txt, log). Tras subirlos, se borran de la bandeja.

La bandeja tiene que estar fuera de `/home` si usas el modo servicio
(`ProtectHome=true`). `setup_service.sh` la crea en
`/srv/jira-gateway-inbox-<empresa>`, con propiedad de tu usuario y grupo
del servicio (`2770`), y la añade al `.env`. Si un fichero llega sin
permiso de lectura para el grupo (por ejemplo, copiado desde uno con modo
`600`), la tool lo dice: `chmod g+r` y listo.

En modo stdio el gateway corre con tu usuario, así que la bandeja puede
estar donde quieras, también en tu `/home`:

```bash
mkdir -p ~/jira-inbox
echo "JIRA_ATTACHMENTS_DIR=$HOME/jira-inbox" >> ~/.secrets/jira-gateway.env
```

Permisos en Jira: el usuario del token necesita el permiso de proyecto
*Create attachments*. Con el scope clásico `write:jira-work` basta; con
scopes granulares hace falta además `write:attachment:jira`.

Bitbucket es **opcional**: si no añades estas variables, el gateway arranca
igual solo con Jira, y las tools de Bitbucket devuelven un error explicando
qué falta. Ver las secciones
[Bitbucket Cloud (pull requests)](#bitbucket-cloud-pull-requests) y
[Bitbucket Pipelines](#bitbucket-pipelines) más abajo para cómo sacar el
token.

```bash
BITBUCKET_EMAIL=tu-email@dominio.com
BITBUCKET_API_TOKEN=el-token-con-los-scopes-de-abajo
BITBUCKET_WORKSPACE=tu-workspace
BITBUCKET_ALLOWED_REPOS=repo-uno,repo-dos  # allowlist dura, sin espacios; vacío = las tools de Bitbucket fallan siempre
BITBUCKET_DEFAULT_REPO=repo-uno  # opcional, repo usado si la tool no recibe repo_slug
BITBUCKET_DEFAULT_TARGET_BRANCH=pre  # opcional, rama destino si la tool no recibe target_branch

# Solo si vas a usar run_pipeline/get_pipeline. Allowlist dura de patrones de
# pipeline custom (separados por comas); vacío = run_pipeline falla siempre.
BITBUCKET_ALLOWED_PIPELINES=sello-version-pre,sello-version-staging,sello-version-prod

# Opcional. Allowlist de revisores explícitos de create_pull_request
# (account_id, separados por comas); vacío = cualquier miembro del workspace.
BITBUCKET_ALLOWED_REVIEWERS=557058:aaaa-...,557058:bbbb-...
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

#### Variante sin clonar el repo (`uvx`)

Si solo quieres usarlo, sin tocar el código, basta con tener
[`uv`](https://docs.astral.sh/uv/) instalado y el
`~/.secrets/jira-gateway.env` de [Configuración](#configuración-variables-de-entorno).
`uvx` descarga el paquete desde git, lo instala en un entorno aislado en
caché (con su propio Python 3.11+ si hace falta) y lanza el comando
`jira-gateway`:

```json
{
  "mcpServers": {
    "jira-gateway": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/lfern/jira-gateway-mcp@v0.4.0", "jira-gateway"]
    }
  }
}
```

Fijar el tag (`@v0.4.0`) evita que cambie de versión sin que lo decidas;
para actualizar, cambia el tag. Si `uvx` no está en el `PATH` que ve
Claude Code, pon la ruta absoluta (normalmente `~/.local/bin/uvx`).

Si ya tienes el repo clonado e instalado en un venv, el mismo comando está
disponible como `/ruta/a/jira-git-gateway/.venv/bin/jira-gateway`
(equivalente a `python -m gateway.server`, que sigue funcionando).

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

### Añadir adjuntos a una instalación ya desplegada

Aquí sí basta con volver a lanzar el script: crea la bandeja
`/srv/jira-gateway-inbox-<empresa>` y, si el `.env` no tiene ya
`JIRA_ATTACHMENTS_DIR`, añade las variables de
[Adjuntos](#adjuntos-opcional). Luego reinicia el servicio:

```bash
sudo bash scripts/setup_service.sh <empresa>
sudo grep -E 'ATTACHMENTS|WSL_DISTRO' /opt/jira-gateway-<empresa>/.env   # comprueba que están
sudo systemctl restart jira-gateway-<empresa>.service
```

En Claude Code, reconecta el MCP (`/mcp`) o abre una sesión nueva para que
vea `add_attachments` y `list_pending_attachments`.

### Añadir Pipelines a una instalación que ya tiene Bitbucket (PRs)

Si tu instancia ya tiene el bloque `BITBUCKET_*` relleno (PRs funcionando)
y quieres activar `run_pipeline`/`get_pipeline`, no hace falta rehacer
nada — solo dos cambios sobre el `.env` que ya tienes:

```bash
sudoedit /opt/jira-gateway-<empresa>/.env
```

1. **Variable nueva** (no existía antes de Pipelines): añade
   `BITBUCKET_ALLOWED_PIPELINES` con los patrones que quieras permitir, ej.
   `BITBUCKET_ALLOWED_PIPELINES=sello-version-pre,sello-version-staging,sello-version-prod`
   — ver [Bitbucket Pipelines](#bitbucket-pipelines).
2. **Variable ya existente que hay que actualizar**: `BITBUCKET_API_TOKEN`
   sigue siendo el mismo token de Bitbucket, pero necesita 2 scopes más
   (`read:pipeline:bitbucket` y `write:pipeline:bitbucket`) — edítalo o
   regenéralo en
   [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens)
   con los 5 scopes juntos (los 3 de PRs + estos 2) y pega el valor nuevo
   aquí. Ver [Notas](#notas) para la lista completa de scopes.

Reinicia para que lo recoja:

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

Si vas a pedir revisores explícitos (`reviewers` en `create_pull_request`),
el gateway también lee los miembros del workspace y tu propio usuario, así
que el token necesita además `read:workspace:bitbucket` y
`read:user:bitbucket`. Sin ellos, solo falla la llamada con `reviewers` (con
un error que dice qué scope falta); el resto sigue igual.

**Ese token lo generas y lo pegas en el `.env` del servicio tú mismo — el
agente nunca lo pide ni lo escribe.**

Tools disponibles (nada de merge, approve, decline ni push — eso lo sigues
haciendo tú o Bitbucket en el momento del merge):

- `create_pull_request(source_branch, title, description, repo_slug=None, target_branch=None, close_source_branch=True, reviewers=None, confirm=False)` —
  mismo protocolo preview→confirm que `create_task`: sin `confirm` devuelve
  exactamente lo que se enviaría (repo, rama origen, rama destino, título,
  descripción completa, `close_source_branch`) sin tocar Bitbucket; con
  `confirm=True`, si ya hay un PR abierto para esa rama origen no crea uno
  nuevo (devuelve el existente con `already_exists: true`), si no lo crea.
  La descripción la redacta el agente (a partir de los commits de la rama y
  la clave Jira que suele ir en el nombre, ej. `feature/REF-432-...`); el
  gateway no la inventa, solo la transporta. Los default reviewers del
  repo se añaden siempre. Con `reviewers` (lista de account_id, `{uuid}` o
  nickname; el email no vale porque Bitbucket no lo expone) se suman
  revisores explícitos, sin duplicados: se buscan entre los miembros del
  workspace y el preview los enseña ya resueltos (nombre y `account_id`),
  junto con los default reviewers que se van a sumar. Un revisor que no
  existe, o que no está en `BITBUCKET_ALLOWED_REVIEWERS` (si la defines),
  hace fallar la tool sin crear nada. Si el autor del PR está en la lista,
  se quita y se avisa en `warnings`. Si ya había un PR abierto para la
  rama, se devuelve ese sin tocar sus revisores, con un aviso. La respuesta
  incluye `reviewers` (nombre y `account_id`).
- `list_open_pull_requests(repo_slug=None, source_branch=None)` — solo
  lectura, PRs abiertos del repo (por defecto `BITBUCKET_DEFAULT_REPO`). Con
  `source_branch`, solo los que salen de esa rama exacta (el filtro lo hace
  Bitbucket, sin el límite de 50).
- `get_pull_request(pr_id, repo_slug=None)` — solo lectura, detalle de un PR
  por id, con sus `reviewers` (nombre y `account_id`).

`repo_slug` está siempre restringido a la allowlist `BITBUCKET_ALLOWED_REPOS`
del `.env` — un repo fuera de esa lista falla sin tocar Bitbucket, y una
allowlist vacía hace fallar las tools siempre (nunca "todos los repos del
workspace").

## Bitbucket Pipelines

Además de PRs, el gateway puede lanzar **pipelines custom** ya definidos en
el `bitbucket-pipelines.yml` de cada repo (los que van bajo la clave
`custom:`) — pensado para pasos operativos acotados (ej. "sello de
versión") lanzados por el agente justo después de un tag o un merge, **no**
para desplegar código a mano ni para ejecutar cualquier pipeline del
fichero.

El mismo token de arriba necesita dos scopes más para esto:

- `read:pipeline:bitbucket`
- `write:pipeline:bitbucket`

Si tu token de Bitbucket ya existe y no tiene estos scopes, edítalo (o
crea uno nuevo) en
[id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens)
y actualiza `BITBUCKET_API_TOKEN` en el `.env` del servicio.

Tools disponibles (nada de parar, cancelar ni relanzar pipelines — solo
dispararlos de nuevo, uno por llamada):

- `run_pipeline(pattern, ref_name="pre", ref_type="branch", repo_slug=None, confirm=False)` —
  mismo protocolo preview→confirm que `create_pull_request`: sin `confirm`
  devuelve exactamente lo que se enviaría (repo, pattern, rama/tag) sin
  tocar Bitbucket; con `confirm=True` lo lanza de verdad y devuelve
  `build_number`, `uuid`, `state` y la URL del run. `pattern` está siempre
  restringido a la allowlist **obligatoria** `BITBUCKET_ALLOWED_PIPELINES`
  del `.env` — un pattern fuera de esa lista falla sin tocar Bitbucket, y
  vacía hace fallar la tool siempre (nunca "cualquier pipeline del repo"),
  precisamente para que un agente no pueda disparar por error algo como un
  deploy a producción.
- `get_pipeline(build_number, repo_slug=None)` — solo lectura: `state`
  (PENDING/IN_PROGRESS/COMPLETED) y `result` (SUCCESSFUL/FAILED/..., solo
  cuando ya terminó).

Si te da un 401/403 al llamar a cualquiera de las dos, el mensaje ya te
dice que probablemente falta el scope de Pipelines en el token — no hace
falta ir a adivinarlo desde un 403 pelado.

## Flujo de uso

1. "¿Qué tareas tengo pendientes?" → `list_my_tasks`. "¿Cuáles tengo en
   Selected for Development?" → `list_my_tasks` con
   `statuses=["Selected for Development"]` (igual en `list_unassigned_tasks`).
2. "Crea una tarea para X" → `create_task` (sin `confirm`) → el agente te
   enseña el preview (proyecto, tipo, resumen, descripción, etiquetas) → si
   dices que sí, el agente vuelve a llamar a `create_task` con `confirm=True`
   y los mismos datos → ahí sí se crea. Acepta `labels` opcional (lista de
   strings) para etiquetar el issue al crearlo. La descripción va en
   markdown (títulos, listas, código, negrita, enlaces...) y se convierte al
   formato de Jira.
3. "Selecciona la PROJ-123 para desarrollo" → `start_task` con
   `status="Selected for Development"` (o el nombre exacto de la transición
   intermedia de tu workflow).
4. "Empieza la PROJ-123" → `start_task` sin `status` → transiciona al estado
   de "en progreso" configurado en `JIRA_IN_PROGRESS_STATUS`. "Pasa la
   PROJ-123 a Listo para validar" → `transition_task` con
   `status="Listo para validar"`.
5. "¿De qué va la PROJ-123?" → `get_task` (solo lectura: resumen,
   descripción en markdown, estado, asignado, labels y padre).
6. "Reescribe la descripción de la PROJ-123" → `update_task` (sin
   `confirm`) → el agente te enseña el diff del antes y el después → si
   dices que sí, vuelve a llamar con `confirm=True`. La descripción va en
   markdown y sustituye a la entera; si la actual tiene cosas que markdown
   no representa (menciones, tablas, adjuntos...), el preview avisa de que
   se perderían. Para etiquetas, `add_labels` / `remove_labels`: añaden o
   quitan sin tocar las demás, con el mismo preview.
7. "Asígnate la PROJ-124" (p. ej. una subtarea recién creada) →
   `assign_task` → te la asigna a ti si estaba sin asignar, y así ya puedes
   hacer `start_task`.
8. "¿Qué comentarios tiene la PROJ-123?" → `list_task_comments` (solo
   lectura, autor/fecha/texto en orden cronológico).
9. "Comenta en la PROJ-123 que..." → `add_comment` (sin `confirm`) → el
   agente te enseña el preview del texto → si dices que sí, vuelve a llamar
   con `confirm=True` para publicarlo.
10. "Apunta 1h 30m en la PROJ-123 de ayer" → `add_worklog` con
   `time_spent="1h 30m"` y `started="YYYY-MM-DD"` (sin `confirm`) → preview
   → `confirm=True`. Solo horas y minutos (`2h`, `45m`, `1h 30m`, `1.5h`),
   máximo 12h por entrada y nunca en el futuro. "¿Cuánto tiempo hay
   apuntado en la PROJ-123?" → `list_worklogs` (solo lectura; marca las
   entradas tuyas con `mine`). Para corregir o borrar una entrada,
   `update_worklog` / `delete_worklog` con su `worklog_id` y el mismo
   preview → confirm. Jira resta el tiempo de la estimación restante, igual
   que al apuntarlo desde la web.
11. "Adjunta esta captura a la PROJ-123" → el agente la copia a la bandeja
   (`list_pending_attachments` dice dónde está) → `add_attachments` (sin
   `confirm`) → el preview da tamaño, tipo, sha256 y la ruta para abrir
   cada fichero → **ábrelo y míralo antes de aprobar**: una captura puede
   mostrar tokens o datos de clientes, y un adjunto no se puede editar
   (quien siga la tarea recibe un aviso al momento) → `confirm=True` con
   la lista `sha256` del preview. Si el fichero cambió desde el preview, no
   se sube nada. `get_task` muestra los adjuntos que ya tiene la tarea.
12. Claude Code crea la rama, desarrolla, comitea y hace push con sus
   herramientas normales de bash/git — el gateway no interviene en nada de
   esto, y puede seguir leyendo `git log`/`git diff`/`git blame` sin
   restricción alguna.
13. Si Bitbucket está configurado: "Abre el PR de esta rama" →
   `create_pull_request` (sin `confirm`) → el agente te enseña el preview
   (repo, rama origen/destino, título, descripción completa) → si dices que
   sí, vuelve a llamar con `confirm=True` → lo crea, o te devuelve el que ya
   estuviera abierto para esa rama si lo había. Si no está configurado,
   abres el PR a mano como hasta ahora.
14. "¿Qué PRs hay abiertos en refunder-react?" → `list_open_pull_requests`.
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
  ni JQL libre en Jira, ni query BBQL libre en Bitbucket. Los filtros por
  estado (`statuses`) y por rama (`source_branch`) solo aceptan nombres que
  pasan una validación sin comillas, y se insertan entre comillas en una
  query fija: no se puede salir del proyecto ni del repo.
- **Bitbucket con allowlist dura de repos** (`BITBUCKET_ALLOWED_REPOS`):
  el token puede tener acceso a más repos del workspace de los que quieres
  que el agente toque; la allowlist es la barrera, no el scope del token.
- **`create_pull_request` no duplica PRs**: antes de crear, busca si ya hay
  uno abierto para esa rama origen y devuelve ese en vez de crear otro.
- **`run_pipeline` con allowlist dura de patrones** (`BITBUCKET_ALLOWED_PIPELINES`,
  obligatoria): el `bitbucket-pipelines.yml` de un repo puede definir
  pipelines custom que sí despliegan o tocan producción; la tool solo puede
  lanzar los patrones que enumeres explícitamente, nunca "cualquiera que
  exista en el fichero". Tampoco puede parar, cancelar ni relanzar nada —
  solo disparar uno nuevo cada vez.
- **Git queda fuera a propósito**: ya confías en Claude Code para manejar
  git por bash (commits, push, y también lectura de historial cuando lo
  necesitas), así que el gateway no intenta duplicar ni restringir eso —
  solo cubre lo que el agente no puede hacer solo, que es hablar con Jira
  sin ver tu token.
- **Validación de issue_key** (`PROJ-123`) antes de tocar Jira.
- **`create_task` nunca escribe en la primera llamada**: el flag `confirm`
  empieza en `False` por defecto, así que la ruta "segura" (preview) es la
  que sale sin que nadie tenga que acordarse de pedirla explícitamente.
- **`assign_task` solo te asigna a ti, y solo tareas sin asignar**: la tool
  no recibe ningún destinatario, así que no hay forma de asignar a otra
  persona; y si la tarea ya es de alguien, falla sin tocar nada. Esa regla
  la aplica el gateway, no depende de que el agente la respete.
  (Jira no tiene asignación condicional: queda una ventana de milisegundos
  entre comprobar y asignar en la que no se puede garantizar al 100%.)
- **Solo issues del proyecto configurado**: además del prefijo de la clave,
  `get_task`, `update_task`, `assign_task` y `start_task` comprueban el
  proyecto que devuelve Jira, porque Jira sigue resolviendo claves antiguas
  de issues movidos a otro proyecto.
- **Tiempo dedicado: apuntar en cualquier tarea, corregir y borrar solo lo
  tuyo.** `update_worklog` y `delete_worklog` comprueban el autor de la
  entrada contra el usuario del token antes de tocar nada. La duración la
  interpreta el gateway (no Jira), con un máximo de 12h por entrada, para
  que el preview muestre exactamente lo que se guarda y un "30h" en vez
  de "3h" no pase.
- **Adjuntos solo desde la bandeja, por nombre.** Ver
  [Adjuntos](#adjuntos-opcional). El sha256 del preview se comprueba al
  confirmar, así que se sube exactamente lo que revisaste.
- **`start_task` y `transition_task` solo transicionan issues asignados a
  ti**, y si defines `JIRA_ALLOWED_STATUSES`, solo a esos estados (así el
  agente no puede cerrar ni rechazar tareas). Comprueban el
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
- Scopes del token de Bitbucket (uno solo, separado del de Jira — ver
  [Configuración](#configuración-variables-de-entorno)): `read:repository:bitbucket`,
  `read:pullrequest:bitbucket` y `write:pullrequest:bitbucket` para PRs
  (más `read:workspace:bitbucket` y `read:user:bitbucket` para pedir
  revisores explícitos) ([detalle](#bitbucket-cloud-pull-requests)); si además vas a usar
  `run_pipeline`/`get_pipeline`, añade `read:pipeline:bitbucket` y
  `write:pipeline:bitbucket` ([detalle](#bitbucket-pipelines)). Las app
  passwords de Bitbucket están retiradas (fin: 28-jul-2026) — el token se
  crea en [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens).
