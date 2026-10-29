"""
Punto de entrada del gateway.

Dos modos, elegidos por MCP_TRANSPORT:
- stdio (por defecto): Claude Code lo lanza como subproceso. El proceso
  hereda las credenciales del entorno de quien lo lanza — solo vale como
  aislamiento si nadie con acceso a esa sesión puede leer el .env, que no es
  el caso cuando el propio agente tiene Bash en la misma máquina/usuario.
- streamable-http: pensado para correr como servicio systemd bajo un
  usuario Unix separado (ver scripts/setup_service.sh). Ahí sí el .env con
  las credenciales de Jira vive fuera del alcance de quien lance Claude
  Code, y el agente solo puede hablar con las tools por red, nunca leer el
  token directamente.

Alcance deliberadamente acotado: Jira y (opcional) Bitbucket Cloud —
pull requests y lanzar pipelines custom ya en su allowlist. El resto de
git (rama, commits, push, historial) lo maneja Claude Code directamente
por bash, como ya hace — este gateway no intenta ser una barrera para
git, solo cubre lo que el agente no puede hacer por sí mismo (hablar con
Jira/Bitbucket con tus credenciales sin que el agente las vea). Bitbucket
es opcional: si el .env no trae BITBUCKET_EMAIL/BITBUCKET_API_TOKEN/
BITBUCKET_WORKSPACE, el servicio arranca igual solo con Jira y las tools
de Bitbucket devuelven un error explicando qué falta.
"""
import difflib
import os

from mcp.server.fastmcp import FastMCP

from .adf import adf_to_markdown, markdown_to_adf
from .attachments import AttachmentError, inbox_from_config, validate_filenames
from .bitbucket_client import BitbucketClient, BitbucketError
from .config import Config, ConfigError
from .jira_client import (
    JiraClient,
    JiraError,
    format_duration,
    parse_duration,
    parse_started,
    validate_issue_key,
    validate_labels,
)

mcp = FastMCP(
    "jira-gateway",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8765")),
)

try:
    _cfg = Config.from_env()
except ConfigError as e:
    raise SystemExit(f"Config inválida: {e}")

_jira = JiraClient(_cfg)
_inbox = inbox_from_config(
    _cfg.jira_attachments_dir, _cfg.jira_attachments_max_mb, _cfg.jira_attachments_windows_dir
)
_bitbucket = BitbucketClient(_cfg) if _cfg.bitbucket_configured else None


@mcp.tool()
def list_my_tasks(statuses: list[str] | None = None) -> list[dict]:
    """Lista tus tareas asignadas y no cerradas en el proyecto configurado.
    Con `statuses` (ej. ['Selected for Development']), solo las que estén en
    alguno de esos estados (nombre exacto del estado en Jira). Solo lectura.
    No acepta JQL: el filtro está fijado y solo admite nombres de estado."""
    try:
        return _jira.list_my_tasks(statuses)
    except JiraError as e:
        return [{"error": str(e)}]


@mcp.tool()
def list_unassigned_tasks(statuses: list[str] | None = None) -> list[dict]:
    """Lista las tareas sin asignar y no cerradas en el proyecto configurado.
    Con `statuses`, solo las que estén en alguno de esos estados (nombre
    exacto del estado en Jira). Solo lectura. No acepta JQL: el filtro está
    fijado y solo admite nombres de estado."""
    try:
        return _jira.list_unassigned_tasks(statuses)
    except JiraError as e:
        return [{"error": str(e)}]


@mcp.tool()
def create_task(
    summary: str,
    description: str = "",
    issue_type: str | None = None,
    labels: list[str] | None = None,
    parent_key: str | None = None,
    confirm: bool = False,
) -> dict:
    """Crea una tarea nueva en Jira en el proyecto configurado. Tras crearla
    la mueve automáticamente al estado JIRA_SELECTED_STATUS (por defecto
    'Selected for Development'), así no se queda parada en Backlog.

    La descripción se pasa en markdown y se convierte a formato Jira.

    Si se pasa `parent_key` (ej. 'PROJ-123'), la tarea se crea como
    subtarea de ese issue. En ese caso, si no se especifica `issue_type`,
    se usa JIRA_SUBTASK_ISSUE_TYPE (por defecto 'Subtask') en lugar del
    tipo por defecto.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    crea nada, solo devuelve una vista previa de lo que se enviaría —
    muéstrasela al usuario tal cual. Solo si el usuario la aprueba,
    vuelve a llamar con confirm=True para crearla de verdad."""
    resolved_type = issue_type or (_cfg.subtask_issue_type if parent_key else _cfg.default_issue_type)
    resolved_labels = labels or []

    if not confirm:
        return {
            "preview": True,
            "project": _cfg.project_key,
            "issue_type": resolved_type,
            "summary": summary,
            "description": description,
            "labels": resolved_labels,
            "parent_key": parent_key,
            "status_after_create": _cfg.selected_status,
            "note": (
                "Nada se ha enviado a Jira todavía. Revisa este preview con "
                "el usuario y, si lo aprueba, llama de nuevo con confirm=True "
                "y los mismos datos. Al crearla, se moverá automáticamente a "
                f"'{_cfg.selected_status}' (no se queda en Backlog)."
            ),
        }

    try:
        result = _jira.create_issue(summary, description, resolved_type, resolved_labels, parent_key)
    except JiraError as e:
        return {"error": str(e)}

    try:
        _jira.transition_issue(result["key"], _cfg.selected_status)
    except JiraError as e:
        return {
            "created": True,
            **result,
            "warning": (
                f"Creada pero no se pudo mover a '{_cfg.selected_status}': {e}. "
                "Ha quedado en el estado inicial por defecto (normalmente Backlog)."
            ),
        }

    return {"created": True, **result, "status": _cfg.selected_status}



@mcp.tool()
def start_task(issue_key: str, status: str | None = None) -> dict:
    """Transiciona el issue indicado en Jira. Sin `status`, lo pasa al estado
    de 'en progreso' configurado (JIRA_IN_PROGRESS_STATUS). Con `status`,
    transiciona a ese estado en su lugar (debe coincidir, sin distinguir
    mayúsculas, con el nombre de una transición disponible o del estado al
    que lleva, ej. 'Selected for Development') — útil para pasos
    previos a empezar a desarrollar. Solo funciona si el issue está asignado
    a ti; si no, devuelve error sin tocar nada. No toca git: la rama, el
    desarrollo, el commit y el push los gestiona Claude Code directamente con
    sus herramientas de siempre."""
    return _transition_mine(issue_key, status or _cfg.in_progress_status)


@mcp.tool()
def transition_task(issue_key: str, status: str) -> dict:
    """Cambia el estado de un issue en Jira (ej. a 'Listo para validar' al
    terminar). `status` puede ser el nombre del estado destino o el de la
    transición (sin distinguir mayúsculas); si no hay ninguna que encaje, el
    error lista las disponibles como 'transición → estado'. Solo funciona si el issue está
    asignado a ti. Si el .env define JIRA_ALLOWED_STATUSES, solo acepta esos
    estados (más los de 'en progreso' y 'seleccionada' configurados)."""
    return _transition_mine(issue_key, status)


def _transition_mine(issue_key: str, status: str) -> dict:
    try:
        _jira.assert_assigned_to_me(issue_key)
        _jira.transition_issue(issue_key, status)
        return {"issue_key": issue_key, "status": status}
    except JiraError as e:
        return {"error": str(e)}


@mcp.tool()
def assign_task(issue_key: str) -> dict:
    """Te asigna a ti (el usuario del gateway) un issue del proyecto
    configurado que esté SIN asignar — por ejemplo una subtarea recién
    creada, para poder hacer start_task después. No acepta otro
    destinatario: no puede asignar a terceros ni quitarle la tarea a quien
    ya la tenga (en ese caso devuelve error sin tocar nada). Si ya es tuya,
    no hace nada y lo indica."""
    try:
        return _jira.assign_to_me(issue_key)
    except JiraError as e:
        return {"error": str(e)}


@mcp.tool()
def get_task(issue_key: str) -> dict:
    """Detalle de un issue del proyecto configurado: resumen, descripción
    (en markdown), estado, persona asignada, labels, padre y adjuntos
    (nombre, tamaño, tipo, autor). Solo lectura.
    Si la descripción tiene elementos sin equivalente en markdown
    (menciones, tablas, adjuntos...), vienen aplanados a texto y se avisa
    en `description_warning`."""
    try:
        task = _jira.get_issue(issue_key)
    except JiraError as e:
        return {"error": str(e)}

    unsupported = task.pop("description_unsupported")
    if unsupported:
        task["description_warning"] = _unsupported_warning(unsupported)
    return task


def _unsupported_warning(unsupported: list[str]) -> str:
    return (
        "La descripción en Jira contiene elementos que no se pueden "
        f"representar en markdown ({', '.join(unsupported)}); aquí aparecen "
        "aplanados a texto. Si se reescribe la descripción, se perderán."
    )


def _diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), "antes", "después", lineterm=""
        )
    )


@mcp.tool()
def update_task(
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    """Edita el resumen, la descripción y/o las etiquetas de un issue del
    proyecto configurado. La descripción se pasa en markdown (como devuelve
    get_task) y se convierte a formato Jira; sustituye a la descripción
    entera, así que parte de la actual (get_task) y modifica solo lo
    necesario. Las etiquetas no se sustituyen: `add_labels` añade y
    `remove_labels` quita, sin tocar las demás. Lo que no se pase no se toca.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    envía nada, solo devuelve una vista previa con el diff del antes y el
    después — muéstrasela al usuario tal cual. Solo si el usuario la
    aprueba, vuelve a llamar con confirm=True y los mismos datos para
    guardarlo de verdad."""
    add_labels = add_labels or []
    remove_labels = remove_labels or []
    if summary is None and description is None and not add_labels and not remove_labels:
        return {"error": "indica al menos summary, description, add_labels o remove_labels"}
    if summary is not None and not summary.strip():
        return {"error": "el resumen no puede quedar vacío"}
    if set(add_labels) & set(remove_labels):
        return {"error": "la misma etiqueta no puede estar en add_labels y remove_labels"}

    try:
        validate_labels([*add_labels, *remove_labels])
        current = _jira.get_issue(issue_key)
    except JiraError as e:
        return {"error": str(e)}

    changes = {}
    if summary is not None and summary != current["summary"]:
        changes["summary"] = {"before": current["summary"], "after": summary}
    if description is not None:
        # Normalizamos con la misma conversión que se enviará, para que el
        # diff muestre exactamente lo que quedará en Jira.
        after, _ = adf_to_markdown(markdown_to_adf(description))
        if after != current["description"]:
            changes["description_diff"] = _diff(current["description"], after)
            if current["description_unsupported"]:
                changes["description_warning"] = _unsupported_warning(
                    current["description_unsupported"]
                )
    to_add = [l for l in dict.fromkeys(add_labels) if l not in current["labels"]]
    to_remove = [l for l in dict.fromkeys(remove_labels) if l in current["labels"]]
    if to_add or to_remove:
        after_labels = [l for l in current["labels"] if l not in to_remove] + to_add
        changes["labels"] = {"before": current["labels"], "after": after_labels}

    if not changes:
        return {"issue_key": issue_key, "no_changes": True, "note": "No hay nada distinto de lo que ya hay en Jira."}

    if not confirm:
        return {
            "preview": True,
            "issue_key": issue_key,
            **changes,
            "note": (
                "Nada se ha enviado a Jira todavía. Revisa este preview con "
                "el usuario y, si lo aprueba, llama de nuevo con confirm=True "
                "y los mismos datos."
            ),
        }

    try:
        _jira.update_issue(
            issue_key,
            summary if "summary" in changes else None,
            description if "description_diff" in changes else None,
            to_add,
            to_remove,
        )
    except JiraError as e:
        return {"error": str(e)}

    return {"updated": True, "issue_key": issue_key, "url": current["url"], **changes}


@mcp.tool()
def list_task_comments(issue_key: str) -> list[dict]:
    """Lista los comentarios de un issue (autor, fecha, texto), en orden
    cronológico. Solo lectura."""
    try:
        return _jira.get_comments(issue_key)
    except JiraError as e:
        return [{"error": str(e)}]


@mcp.tool()
def add_comment(issue_key: str, body: str, confirm: bool = False) -> dict:
    """Añade un comentario a un issue de Jira. El texto va en markdown
    (títulos, listas, negrita, código, enlaces...) y se convierte al
    formato de Jira; las tablas no se soportan y salen como texto.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    envía nada, solo devuelve una vista previa del comentario — muéstrasela
    al usuario tal cual. Solo si el usuario la aprueba, vuelve a llamar con
    confirm=True y los mismos datos para publicarlo de verdad."""
    if not confirm:
        return {
            "preview": True,
            "issue_key": issue_key,
            "body": body,
            "note": (
                "Nada se ha enviado a Jira todavía. Revisa este preview con "
                "el usuario y, si lo aprueba, llama de nuevo con confirm=True "
                "y los mismos datos."
            ),
        }

    try:
        result = _jira.add_comment(issue_key, body)
    except JiraError as e:
        return {"error": str(e)}

    return {"created": True, "issue_key": issue_key, **result}


# --- adjuntos ------------------------------------------------------------------------
#
# Solo se suben ficheros de la bandeja (JIRA_ATTACHMENTS_DIR), por nombre y
# nunca por ruta: ver gateway/attachments.py.


def _attachments_unavailable() -> dict:
    return {
        "error": (
            "adjuntos no configurados: falta JIRA_ATTACHMENTS_DIR en el .env "
            "(la bandeja de la que se suben los ficheros)."
        )
    }


@mcp.tool()
def list_pending_attachments() -> dict:
    """Muestra la bandeja de adjuntos: la carpeta donde hay que copiar un
    fichero (con `install -m 644 <fichero> <bandeja>/`) antes de subirlo
    con `add_attachments`, y los ficheros que hay ahora en ella. Solo
    lectura."""
    if _inbox is None:
        return _attachments_unavailable()
    try:
        files = _inbox.list()
    except AttachmentError as e:
        return {"error": str(e)}
    return {
        "dir": _inbox.directory,
        "windows_dir": _inbox.windows_dir,
        "max_bytes": _inbox.max_bytes,
        "files": files,
    }


@mcp.tool()
def add_attachments(
    issue_key: str,
    filenames: list[str],
    sha256: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    """Adjunta ficheros (capturas, PDFs, logs) a un issue del proyecto.

    Los ficheros tienen que estar en la bandeja de adjuntos: pide su ruta
    con `list_pending_attachments`, cópialos ahí primero con
    `install -m 644 <fichero> <bandeja>/` (así el gateway puede leerlos) y
    pasa solo el nombre, nunca una ruta. Tipos: png, jpg, gif, webp, pdf, txt, log.
    Tras subirlos, se borran de la bandeja.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    envía nada, solo devuelve una vista previa con el tamaño, el tipo y el
    sha256 de cada fichero, y la ruta para abrirlo. Pide al usuario que
    ABRA Y MIRE cada fichero antes de aprobar (una captura puede mostrar
    tokens o datos de clientes), no basta con el nombre. Solo si lo
    aprueba, vuelve a llamar con confirm=True, los mismos ficheros y la
    lista `sha256` del preview, en el mismo orden: si un fichero ha
    cambiado desde el preview, no se sube nada."""
    if _inbox is None:
        return _attachments_unavailable()

    try:
        validate_issue_key(issue_key, _cfg.project_key)
        names = validate_filenames(filenames)
        files = [_inbox.read(n) for n in names]
    except (AttachmentError, JiraError) as e:
        return {"error": str(e)}

    if not confirm:
        return {
            "preview": True,
            "issue_key": issue_key,
            "files": [
                {**f.summary(), "path": _inbox.path(f.filename), "windows_path": _inbox.windows_path(f.filename)}
                for f in files
            ],
            "sha256": [f.sha256 for f in files],
            "note": (
                "Nada se ha enviado a Jira todavía. Pide al usuario que abra y "
                "revise cada fichero (no solo el nombre) y, si lo aprueba, llama "
                "de nuevo con confirm=True, los mismos ficheros y esta lista "
                "sha256. Un adjunto no se puede editar, y quien siga la tarea "
                "recibe un aviso al momento."
            ),
        }

    if sha256 is None or len(sha256) != len(files):
        return {"error": "con confirm=True hay que pasar la lista sha256 del preview, un valor por fichero."}
    changed = [f.filename for f, expected in zip(files, sha256) if f.sha256 != expected]
    if changed:
        return {
            "error": (
                f"estos ficheros han cambiado desde el preview: {', '.join(changed)}. "
                "No se ha subido nada; vuelve a pedir el preview y que el usuario lo revise."
            )
        }

    try:
        attachments = _jira.add_attachments(issue_key, [(f.filename, f.content, f.mime_type) for f in files])
    except JiraError as e:
        return {"error": str(e)}

    result = {
        "created": True,
        "issue_key": issue_key,
        "url": f"{_cfg.jira_site_url}/browse/{issue_key}",
        "attachments": attachments,
    }
    not_removed = []
    for f in files:
        try:
            _inbox.remove(f.filename)
        except (AttachmentError, OSError):
            not_removed.append(f.filename)
    if not_removed:
        result["warning"] = (
            f"subidos, pero no pude borrarlos de la bandeja: {', '.join(not_removed)}. "
            "Bórralos a mano para no volver a subirlos por error."
        )
    return result


_WORKLOG_ESTIMATE_NOTE = (
    "Jira restará este tiempo de la estimación restante de la tarea, igual "
    "que al apuntarlo desde la web."
)


@mcp.tool()
def list_worklogs(issue_key: str) -> list[dict]:
    """Lista el tiempo apuntado en un issue del proyecto configurado: id,
    autor, si es tuyo (`mine`), inicio, duración y comentario. Solo
    lectura. El `id` es el que piden update_worklog y delete_worklog."""
    try:
        return _jira.list_worklogs(issue_key)
    except JiraError as e:
        return [{"error": str(e)}]


@mcp.tool()
def add_worklog(
    issue_key: str,
    time_spent: str,
    started: str | None = None,
    comment: str = "",
    confirm: bool = False,
) -> dict:
    """Apunta tiempo dedicado en cualquier issue del proyecto configurado.

    `time_spent` en horas y/o minutos: '2h', '45m', '1h 30m' o '1.5h' (sin
    días: dependen de la config de Jira). Máximo 12h por entrada.
    `started` es cuándo empezaste: 'YYYY-MM-DD' (se toma a las 09:00) o
    'YYYY-MM-DDTHH:MM', en hora local; sin valor, ahora. No admite fechas
    futuras. Jira resta el tiempo de la estimación restante de la tarea,
    igual que al apuntarlo desde la web.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    envía nada, solo devuelve una vista previa — muéstrasela al usuario tal
    cual. Solo si el usuario la aprueba, vuelve a llamar con confirm=True y
    los mismos datos para guardarlo de verdad."""
    try:
        seconds = parse_duration(time_spent)
        resolved_started = parse_started(started)
    except JiraError as e:
        return {"error": str(e)}

    if not confirm:
        return {
            "preview": True,
            "issue_key": issue_key,
            "time_spent": format_duration(seconds),
            "started": resolved_started,
            "comment": comment,
            "note": (
                "Nada se ha enviado a Jira todavía. Revisa este preview con "
                "el usuario y, si lo aprueba, llama de nuevo con confirm=True "
                f"y los mismos datos. {_WORKLOG_ESTIMATE_NOTE}"
            ),
        }

    try:
        result = _jira.add_worklog(issue_key, seconds, resolved_started, comment)
    except JiraError as e:
        return {"error": str(e)}

    return {"created": True, "issue_key": issue_key, **result, "time_spent": format_duration(seconds)}


@mcp.tool()
def update_worklog(
    issue_key: str,
    worklog_id: str,
    time_spent: str | None = None,
    started: str | None = None,
    comment: str | None = None,
    confirm: bool = False,
) -> dict:
    """Corrige una entrada de tiempo TUYA (duración, inicio y/o comentario).
    Si la entrada es de otra persona, devuelve error sin tocar nada. Mismo
    formato que add_worklog; lo que no se pase no se toca. Saca el
    `worklog_id` de list_worklogs.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    envía nada, solo devuelve una vista previa con el antes y el después —
    muéstrasela al usuario tal cual. Solo si el usuario la aprueba, vuelve a
    llamar con confirm=True y los mismos datos para guardarlo de verdad."""
    if time_spent is None and started is None and comment is None:
        return {"error": "indica al menos time_spent, started o comment"}
    try:
        seconds = parse_duration(time_spent) if time_spent is not None else None
        resolved_started = parse_started(started) if started is not None else None
        current = _jira.get_my_worklog(issue_key, worklog_id)
    except JiraError as e:
        return {"error": str(e)}

    changes = {}
    if seconds is not None and seconds != current["time_spent_seconds"]:
        changes["time_spent"] = {"before": current["time_spent"], "after": format_duration(seconds)}
    if resolved_started is not None and resolved_started != current["started"]:
        changes["started"] = {"before": current["started"], "after": resolved_started}
    if comment is not None and comment != current["comment"]:
        changes["comment"] = {"before": current["comment"], "after": comment}

    if not changes:
        return {"issue_key": issue_key, "worklog_id": current["id"], "no_changes": True}

    if not confirm:
        return {
            "preview": True,
            "issue_key": issue_key,
            "worklog_id": current["id"],
            **changes,
            "note": (
                "Nada se ha enviado a Jira todavía. Revisa este preview con "
                "el usuario y, si lo aprueba, llama de nuevo con confirm=True "
                "y los mismos datos."
            ),
        }

    try:
        _jira.update_worklog(
            issue_key,
            current["id"],
            seconds if "time_spent" in changes else None,
            resolved_started if "started" in changes else None,
            comment if "comment" in changes else None,
        )
    except JiraError as e:
        return {"error": str(e)}

    return {"updated": True, "issue_key": issue_key, "worklog_id": current["id"], **changes}


@mcp.tool()
def delete_worklog(issue_key: str, worklog_id: str, confirm: bool = False) -> dict:
    """Borra una entrada de tiempo TUYA (p. ej. apuntada en la tarea
    equivocada). Si la entrada es de otra persona, devuelve error sin tocar
    nada. Saca el `worklog_id` de list_worklogs.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    borra nada, solo devuelve la entrada que se borraría — muéstrasela al
    usuario tal cual. Solo si el usuario lo aprueba, vuelve a llamar con
    confirm=True para borrarla de verdad."""
    try:
        current = _jira.get_my_worklog(issue_key, worklog_id)
    except JiraError as e:
        return {"error": str(e)}

    if not confirm:
        return {
            "preview": True,
            "issue_key": issue_key,
            "worklog": current,
            "note": (
                "Nada se ha borrado todavía. Revisa esta entrada con el "
                "usuario y, si lo aprueba, llama de nuevo con confirm=True."
            ),
        }

    try:
        _jira.delete_worklog(issue_key, current["id"])
    except JiraError as e:
        return {"error": str(e)}

    return {"deleted": True, "issue_key": issue_key, "worklog": current}


def _bitbucket_unavailable() -> dict:
    return {
        "error": (
            "Bitbucket no está configurado en este servicio: falta "
            "BITBUCKET_EMAIL/BITBUCKET_API_TOKEN/BITBUCKET_WORKSPACE en el "
            ".env del servicio."
        )
    }


def _resolve_bb_repo(repo_slug: str | None) -> str | None:
    return repo_slug or _cfg.bitbucket_default_repo


@mcp.tool()
def create_pull_request(
    source_branch: str,
    title: str,
    description: str = "",
    repo_slug: str | None = None,
    target_branch: str | None = None,
    close_source_branch: bool = True,
    reviewers: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    """Crea un pull request en Bitbucket Cloud desde `source_branch` hacia
    `target_branch` (por defecto BITBUCKET_DEFAULT_TARGET_BRANCH, ej.
    'pre'). Si no se pasa `repo_slug`, usa BITBUCKET_DEFAULT_REPO. La
    descripción la escribe el agente que llama (commits de la rama, clave
    Jira, etc.) — esta tool no inventa ninguna plantilla, solo transporta lo
    que se le pasa.

    `reviewers` (opcional): revisores explícitos, cada uno como account_id
    de Bitbucket, {uuid} o nickname (el email no vale: Bitbucket no lo
    expone). Se buscan entre los miembros del workspace; si alguno no
    existe, o el .env define BITBUCKET_ALLOWED_REVIEWERS y no está ahí, la
    tool falla sin crear nada. Si el autor del PR está en la lista, se
    quita y se avisa. Los default reviewers del repo se siguen añadiendo
    siempre, con o sin `reviewers`: los explícitos se suman a ellos, sin
    duplicados.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    crea nada, solo devuelve una vista previa exacta de lo que se enviaría
    (con los revisores ya resueltos: nombre visible y account_id) —
    muéstrasela al usuario tal cual. Solo si el usuario la aprueba, vuelve
    a llamar con confirm=True y los mismos datos para crearlo de verdad. Si
    ya existe un PR abierto para esa rama origen, no crea uno nuevo: te
    devuelve el existente, sin tocar sus revisores. Esta tool NUNCA hace
    merge, approve, decline ni push — solo crea o encuentra el PR."""
    if _bitbucket is None:
        return _bitbucket_unavailable()

    resolved_repo = _resolve_bb_repo(repo_slug)
    if not resolved_repo:
        return {"error": "falta repo_slug (o configura BITBUCKET_DEFAULT_REPO en el .env)."}
    resolved_target = target_branch or _cfg.bitbucket_default_target_branch

    if not confirm:
        preview = {
            "preview": True,
            "repo": resolved_repo,
            "source_branch": source_branch,
            "target_branch": resolved_target,
            "title": title,
            "description": description,
            "close_source_branch": close_source_branch,
        }
        if reviewers:
            try:
                resolution = _bitbucket.resolve_reviewers(resolved_repo, reviewers)
            except BitbucketError as e:
                return {"error": str(e)}
            preview["reviewers"] = resolution["reviewers"]
            preview["default_reviewers"] = resolution["default_reviewers"]
            if resolution["warnings"]:
                preview["warnings"] = resolution["warnings"]
        preview["note"] = (
            "Nada se ha enviado a Bitbucket todavía. Revisa este preview "
            "con el usuario y, si lo aprueba, llama de nuevo con "
            "confirm=True y los mismos datos."
        )
        return preview

    try:
        existing = _bitbucket.find_open_pr_for_branch(resolved_repo, source_branch)
        if existing:
            result = {"already_exists": True, **existing}
            if reviewers:
                result["warnings"] = [
                    "Ya había un PR abierto para esta rama: se devuelve ese y los "
                    "revisores pedidos NO se han aplicado (sus revisores no se tocan)."
                ]
            return result

        resolution = _bitbucket.resolve_reviewers(resolved_repo, reviewers) if reviewers else None
        result = _bitbucket.create_pull_request(
            resolved_repo,
            source_branch,
            resolved_target,
            title,
            description,
            close_source_branch,
            reviewers=resolution["account_ids"] if resolution else None,
        )
    except BitbucketError as e:
        return {"error": str(e)}

    result = {"created": True, **result}
    if resolution and resolution["warnings"]:
        result["warnings"] = resolution["warnings"]
    return result


@mcp.tool()
def list_open_pull_requests(repo_slug: str | None = None, source_branch: str | None = None) -> list[dict]:
    """Lista los pull requests abiertos del repo (por defecto
    BITBUCKET_DEFAULT_REPO). Con `source_branch`, solo los que salen de esa
    rama exacta (lista vacía si no hay ninguno) — útil para comprobar si ya
    hay un PR abierto antes de crearlo. Solo lectura."""
    if _bitbucket is None:
        return [_bitbucket_unavailable()]

    resolved_repo = _resolve_bb_repo(repo_slug)
    if not resolved_repo:
        return [{"error": "falta repo_slug (o configura BITBUCKET_DEFAULT_REPO en el .env)."}]

    try:
        return _bitbucket.list_open_pull_requests(resolved_repo, source_branch)
    except BitbucketError as e:
        return [{"error": str(e)}]


@mcp.tool()
def get_pull_request(pr_id: int, repo_slug: str | None = None) -> dict:
    """Detalle de un pull request de Bitbucket por id (repo por defecto
    BITBUCKET_DEFAULT_REPO si no se indica). Solo lectura."""
    if _bitbucket is None:
        return _bitbucket_unavailable()

    resolved_repo = _resolve_bb_repo(repo_slug)
    if not resolved_repo:
        return {"error": "falta repo_slug (o configura BITBUCKET_DEFAULT_REPO en el .env)."}

    try:
        return _bitbucket.get_pull_request(resolved_repo, pr_id)
    except BitbucketError as e:
        return {"error": str(e)}


@mcp.tool()
def run_pipeline(
    pattern: str,
    ref_name: str = "pre",
    ref_type: str = "branch",
    repo_slug: str | None = None,
    confirm: bool = False,
) -> dict:
    """Lanza un pipeline CUSTOM de Bitbucket (uno de los definidos bajo
    `custom:` en bitbucket-pipelines.yml) sobre una rama o tag. Pensada para
    pasos operativos acotados (ej. sellos de versión), NO para desplegar
    código a mano. Solo puede lanzar patrones que estén en la allowlist
    BITBUCKET_ALLOWED_PIPELINES — si `pattern` no está ahí, falla sin llamar
    a Bitbucket y sin ninguna excepción. Esta tool NUNCA para, cancela ni
    relanza pipelines: cada llamada con confirm=True dispara uno nuevo.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    lanza nada, solo devuelve la vista previa exacta de lo que se enviaría
    (repo, pattern, rama/tag) — muéstrasela al usuario tal cual. Solo si el
    usuario la aprueba, vuelve a llamar con confirm=True y los mismos datos
    para lanzarlo de verdad."""
    if _bitbucket is None:
        return _bitbucket_unavailable()

    resolved_repo = _resolve_bb_repo(repo_slug)
    if not resolved_repo:
        return {"error": "falta repo_slug (o configura BITBUCKET_DEFAULT_REPO en el .env)."}

    if not confirm:
        return {
            "preview": True,
            "repo": resolved_repo,
            "pattern": pattern,
            "ref_name": ref_name,
            "ref_type": ref_type,
            "note": (
                "Nada se ha enviado a Bitbucket todavía. Revisa este preview "
                "con el usuario y, si lo aprueba, llama de nuevo con "
                "confirm=True y los mismos datos."
            ),
        }

    try:
        result = _bitbucket.run_pipeline(resolved_repo, pattern, ref_name, ref_type)
    except BitbucketError as e:
        return {"error": str(e)}

    return {"started": True, **result}


@mcp.tool()
def get_pipeline(build_number: int, repo_slug: str | None = None) -> dict:
    """Estado de un pipeline de Bitbucket por build_number: `state`
    (PENDING/IN_PROGRESS/COMPLETED) y `result` (SUCCESSFUL/FAILED/..., solo
    presente cuando ya ha terminado). Solo lectura."""
    if _bitbucket is None:
        return _bitbucket_unavailable()

    resolved_repo = _resolve_bb_repo(repo_slug)
    if not resolved_repo:
        return {"error": "falta repo_slug (o configura BITBUCKET_DEFAULT_REPO en el .env)."}

    try:
        return _bitbucket.get_pipeline(resolved_repo, build_number)
    except BitbucketError as e:
        return {"error": str(e)}


def main() -> None:
    """Entry point del script `jira-gateway` (ver [project.scripts])."""
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()
