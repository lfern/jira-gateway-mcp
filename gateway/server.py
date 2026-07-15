"""
Punto de entrada del gateway. Arranca vía stdio, pensado para configurarse
como servidor MCP local en Claude Code.

Alcance deliberadamente reducido: SOLO Jira. Git lo maneja Claude Code
directamente por bash, como ya hace — este gateway no intenta ser una
barrera para git, solo cubre lo que el agente no puede hacer por sí mismo
(hablar con Jira con tus credenciales sin que el agente las vea).
"""
from mcp.server.fastmcp import FastMCP

from .config import Config, ConfigError
from .jira_client import JiraClient, JiraError

mcp = FastMCP("jira-gateway")

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
def create_task(summary: str, description: str = "", issue_type: str | None = None, confirm: bool = False) -> dict:
    """Crea una tarea nueva en Jira en el proyecto configurado.

    IMPORTANTE: llama primero SIN `confirm` (o con confirm=False). Eso no
    crea nada, solo devuelve una vista previa de lo que se enviaría —
    muéstrasela al usuario tal cual. Solo si el usuario la aprueba,
    vuelve a llamar con confirm=True para crearla de verdad."""
    resolved_type = issue_type or _cfg.default_issue_type

    if not confirm:
        return {
            "preview": True,
            "project": _cfg.project_key,
            "issue_type": resolved_type,
            "summary": summary,
            "description": description,
            "note": (
                "Nada se ha enviado a Jira todavía. Revisa este preview con "
                "el usuario y, si lo aprueba, llama de nuevo con confirm=True "
                "y los mismos datos."
            ),
        }

    try:
        result = _jira.create_issue(summary, description, resolved_type)
        return {"created": True, **result}
    except JiraError as e:
        return {"error": str(e)}



    """Pasa el issue indicado a 'In Progress' en Jira. No toca git: la rama,
    el desarrollo, el commit y el push los gestiona Claude Code directamente
    con sus herramientas de siempre."""
    try:
        _jira.transition_to_in_progress(issue_key)
        return {"issue_key": issue_key, "status": "in_progress"}
    except JiraError as e:
        return {"error": str(e)}


if __name__ == "__main__":
    mcp.run()
