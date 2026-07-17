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

Alcance deliberadamente reducido: SOLO Jira. Git lo maneja Claude Code
directamente por bash, como ya hace — este gateway no intenta ser una
barrera para git, solo cubre lo que el agente no puede hacer por sí mismo
(hablar con Jira con tus credenciales sin que el agente las vea).
"""
import os

from mcp.server.fastmcp import FastMCP

from .config import Config, ConfigError
from .jira_client import JiraClient, JiraError

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


@mcp.tool()
def list_my_tasks() -> list[dict]:
    """Lista tus tareas asignadas y no cerradas en el proyecto configurado.
    Solo lectura. No acepta JQL ni parámetros: el filtro está fijado."""
    try:
        return _jira.list_my_tasks()
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
    mayúsculas, con el nombre exacto de una transición disponible en el
    workflow del issue, ej. 'Selected for Development') — útil para pasos
    previos a empezar a desarrollar. Solo funciona si el issue está asignado
    a ti; si no, devuelve error sin tocar nada. No toca git: la rama, el
    desarrollo, el commit y el push los gestiona Claude Code directamente con
    sus herramientas de siempre."""
    resolved_status = status or _cfg.in_progress_status
    try:
        _jira.assert_assigned_to_me(issue_key)
        _jira.transition_issue(issue_key, resolved_status)
        return {"issue_key": issue_key, "status": resolved_status}
    except JiraError as e:
        return {"error": str(e)}


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
