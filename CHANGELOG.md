# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/);
el proyecto sigue [versionado semántico](https://semver.org/lang/es/).

Las entradas hasta la 0.3.0 se han reconstruido a partir del historial de
git.

## [Sin publicar]

## [0.5.0] - 2026-10-29

### Añadido
- `list_my_tasks` y `list_unassigned_tasks` aceptan `statuses` opcional
  para filtrar por estado. No es JQL libre: solo nombres de estado
  validados, que entran entre comillas en la query fija.
- `transition_task(issue_key, status)`: cambia el estado de una tarea
  asignada a ti (ej. a «Listo para validar»). Hace lo mismo que
  `start_task` con `status`, pero con un nombre que se encuentra mejor.
- `JIRA_ALLOWED_STATUSES` (opcional): allowlist de estados destino para
  `start_task` y `transition_task`. Los estados configurados de «en
  progreso» y «seleccionada» se permiten siempre.
- `update_task` acepta `add_labels` y `remove_labels`, con el mismo
  preview y confirmación en dos pasos. Añaden o quitan sin tocar las demás.
- `list_open_pull_requests` acepta `source_branch` para buscar el PR
  abierto de una rama.
- Tiempo dedicado (worklogs): `list_worklogs` (solo lectura),
  `add_worklog`, `update_worklog` y `delete_worklog` (confirmación en dos
  pasos). Se puede apuntar en cualquier tarea del proyecto; corregir y
  borrar, solo las entradas propias. Duración en horas/minutos (`1h 30m`),
  máximo 12h por entrada, sin fechas futuras. Jira resta el tiempo de la
  estimación restante (`adjustEstimate=auto`), igual que desde la web.
- `create_pull_request` acepta `reviewers` opcional (account_id, `{uuid}`
  o nickname), que se suman a los default reviewers del repo sin
  duplicados. El preview los enseña ya resueltos; un revisor desconocido
  falla sin crear nada y el autor se quita con aviso. Si ya hay un PR
  abierto, no se tocan sus revisores. Necesita los scopes
  `read:workspace:bitbucket` y `read:user:bitbucket` solo al usar
  `reviewers`.
- `BITBUCKET_ALLOWED_REVIEWERS` (opcional): allowlist de `account_id`
  para los revisores explícitos.
- `create_pull_request` y `get_pull_request` devuelven `reviewers` (nombre
  y `account_id`).
- Adjuntos: `add_attachments` (confirmación en dos pasos) sube ficheros a
  una tarea, y `list_pending_attachments` muestra la bandeja. Solo sube
  ficheros de la bandeja (`JIRA_ATTACHMENTS_DIR`), por nombre y nunca por
  ruta, para que el gateway no pueda subir su propio `.env`. Sin enlaces
  simbólicos ni hard links, tipos fijos (png, jpg, gif, webp, pdf, txt,
  log) y tamaño máximo (`JIRA_ATTACHMENTS_MAX_MB`, 10 por defecto). El
  preview da el sha256 de cada fichero y al confirmar se comprueba de
  nuevo. Tras subirlos, se borran de la bandeja.
- `get_task` incluye los adjuntos de la tarea.
- `setup_service.sh` crea la bandeja `/srv/jira-gateway-inbox-<empresa>`
  y le da acceso al servicio.

### Cambiado
- Las transiciones (`start_task`, `transition_task`, y el paso automático
  de `create_task`) aceptan el nombre del estado destino además del de la
  transición: en muchos workflows no coinciden (ej. «Devolver a X» lleva a
  «X»). Si no encaja ninguna, el error lista «transición → estado».
- `create_pull_request` busca el PR existente de la rama con un filtro en
  Bitbucket, así que ya no se le escapa si hay más de 50 PRs abiertos.
- `create_task` y `update_task` rechazan etiquetas con espacios antes de
  llamar a Jira.

### Corregido
- `add_comment` convierte el texto de markdown al formato de Jira (ADF),
  igual que las descripciones. Antes iba todo en un solo párrafo de texto
  plano, así que listas, títulos y `**` salían como caracteres sueltos.
- `create_pull_request` ya no toma un PR mergeado como el PR abierto de la
  rama: con `q`, Bitbucket ignora el parámetro `state`, así que el estado
  va ahora dentro de la query.

## [0.4.0] - 2026-10-09

### Añadido
- `get_task(issue_key)`: detalle de un issue del proyecto configurado
  (resumen, descripción en markdown, estado, asignado, labels y padre).
  Solo lectura.
- `update_task(issue_key, summary?, description?, confirm=False)`: edita el
  resumen y/o la descripción con confirmación en dos pasos; la vista previa
  muestra el diff del antes y el después. Si la descripción actual tiene
  elementos que markdown no puede representar (menciones, tablas,
  adjuntos...), avisa de que se perderían.
- `assign_task(issue_key)`: te asigna a ti un issue que esté sin asignar
  (por ejemplo, una subtarea recién creada, para poder hacer `start_task`).
  No acepta destinatario y falla si la tarea ya es de otra persona: la regla
  la aplica el gateway, no el agente.
- Conversor Markdown ↔ ADF propio (`gateway/adf.py`), sin dependencias
  nuevas: párrafos, títulos, listas anidadas, bloques de código, citas,
  separadores, negrita, cursiva, tachado, código y enlaces.
- Tests de las tools de Jira y del conversor.

### Cambiado
- `create_task` interpreta la descripción como markdown y la convierte a
  formato Jira. Antes metía el texto tal cual en un único párrafo.
- `get_task`, `update_task`, `assign_task` y `start_task` comprueban el
  proyecto que devuelve Jira, no solo el prefijo de la clave: Jira sigue
  resolviendo claves antiguas de issues movidos a otro proyecto.

### Problemas conocidos
- `assign_task`: Jira no tiene asignación condicional, así que entre
  comprobar que la tarea está libre y asignarla queda una ventana de
  milisegundos en la que no se puede garantizar al 100%.

## [0.3.0] - 2026-09-29

### Añadido
- Comentarios de Jira: `list_task_comments` y `add_comment` (este con
  confirmación en dos pasos).
- `list_unassigned_tasks`: tareas abiertas sin asignar del proyecto.
- Bitbucket Cloud (opcional): `create_pull_request` (confirmación en dos
  pasos; si ya hay un PR abierto para la rama, devuelve ese),
  `list_open_pull_requests` y `get_pull_request`, con allowlist dura de
  repos (`BITBUCKET_ALLOWED_REPOS`).
- Pipelines de Bitbucket: `run_pipeline` (confirmación en dos pasos,
  allowlist obligatoria `BITBUCKET_ALLOWED_PIPELINES`) y `get_pipeline`.
- Despliegue de varias empresas en la misma máquina: un usuario, una
  carpeta y una unidad systemd por empresa, y `scripts/jira-switch.sh` para
  alternar cuál está activa sin tocar la config MCP de Claude Code.
- Entry point `jira-gateway`, para lanzarlo con `uvx` sin clonar el repo.
- Tests con pytest y responses.

### Corregido
- `setup_service.sh` reinstala el Python gestionado antes de crear el venv,
  para no arrastrar uno corrupto de un redeploy fallido anterior.

## [0.2.0] - 2026-07-17

### Añadido
- `create_task` acepta `labels` y mueve el issue a `JIRA_SELECTED_STATUS`
  al crearlo.
- `create_task` acepta `parent_key` para crear subtareas
  (`JIRA_SUBTASK_ISSUE_TYPE`, por defecto `Subtask`).
- `start_task` acepta un `status` opcional y solo transiciona issues
  asignados al usuario del token.
- Despliegue como servicio systemd bajo un usuario Unix separado
  (`scripts/setup_service.sh`), para que el token no sea legible desde la
  sesión de Claude Code.
- `scripts/smoke_test.py` para validar las credenciales sin pasar por MCP.
- `.env.example` con todas las variables soportadas.

### Cambiado
- Las credenciales se leen de `~/.secrets/jira-gateway.env` (o de
  `JIRA_GATEWAY_ENV_FILE`) en vez de un `.env` dentro del proyecto, para
  poder bloquear esa carpeta con reglas de permisos.

### Corregido
- `start_task` no estaba registrado como tool.

## [0.1.0] - 2026-07-15

### Añadido
- Primera versión para Jira Cloud: `list_my_tasks` y `create_task`
  (confirmación en dos pasos), con validación de `issue_key` y sin JQL
  libre. `start_task` estaba escrito pero no quedaba registrado como tool
  (corregido en la 0.2.0).

[Sin publicar]: https://github.com/lfern/jira-gateway-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/lfern/jira-gateway-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/lfern/jira-gateway-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lfern/jira-gateway-mcp/compare/2e32e09...v0.3.0
[0.2.0]: https://github.com/lfern/jira-gateway-mcp/compare/cb281c7...2e32e09
[0.1.0]: https://github.com/lfern/jira-gateway-mcp/commit/cb281c7
