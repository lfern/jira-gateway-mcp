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
from .bitbucket_client import BitbucketClient, BitbucketError
from .config import Config, ConfigError
from .jira_client import JiraClient, JiraError, validate_labels

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
    (en markdown), estado, persona asignada, labels y padre. Solo lectura.
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
    """Añade un comentario a un issue de Jira.

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
    confirm: bool = False,
) -> dict:
    """Crea un pull request en Bitbucket Cloud desde `source_branch` hacia
    `target_branch` (por defecto BITBUCKET_DEFAULT_TARGET_BRANCH, ej.
    'pre'). Si no se pasa `repo_slug`, usa BITBUCKET_DEFAULT_REPO. La
    descripción la escribe el agente que llama (commits de la rama, clave
    Jira, etc.) — esta tool no inventa ninguna plantilla, solo transporta lo
    que se le pasa. No añade reviewers explícitos: si el repo tiene default
    reviewers configurados en Bitbucket, se añaden solos.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    crea nada, solo devuelve una vista previa exacta de lo que se enviaría
    — muéstrasela al usuario tal cual. Solo si el usuario la aprueba, vuelve
    a llamar con confirm=True y los mismos datos para crearlo de verdad. Si
    ya existe un PR abierto para esa rama origen, no crea uno nuevo: te
    devuelve el existente. Esta tool NUNCA hace merge, approve, decline ni
    push — solo crea o encuentra el PR."""
    if _bitbucket is None:
        return _bitbucket_unavailable()

    resolved_repo = _resolve_bb_repo(repo_slug)
    if not resolved_repo:
        return {"error": "falta repo_slug (o configura BITBUCKET_DEFAULT_REPO en el .env)."}
    resolved_target = target_branch or _cfg.bitbucket_default_target_branch

    if not confirm:
        return {
            "preview": True,
            "repo": resolved_repo,
            "source_branch": source_branch,
            "target_branch": resolved_target,
            "title": title,
            "description": description,
            "close_source_branch": close_source_branch,
            "note": (
                "Nada se ha enviado a Bitbucket todavía. Revisa este preview "
                "con el usuario y, si lo aprueba, llama de nuevo con "
                "confirm=True y los mismos datos."
            ),
        }

    try:
        existing = _bitbucket.find_open_pr_for_branch(resolved_repo, source_branch)
        if existing:
            return {"already_exists": True, **existing}

        result = _bitbucket.create_pull_request(
            resolved_repo, source_branch, resolved_target, title, description, close_source_branch
        )
    except BitbucketError as e:
        return {"error": str(e)}

    return {"created": True, **result}


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
