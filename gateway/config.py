"""
Config del gateway. Todo sale de variables de entorno — el agente (Claude Code)
nunca ve estos valores, solo ve las tools que exponemos en server.py.

Se cargan desde un fichero .env FUERA de este repo (por defecto
~/.secrets/jira-gateway.env, o la ruta en JIRA_GATEWAY_ENV_FILE) a
propósito: así el token no está sentado en el propio directorio del
proyecto que Claude Code tiene abierto para trabajar, y ~/.secrets/ puede
bloquearse explícitamente por permisos de Claude Code (ver README). No es
una barrera de seguridad dura por sí sola (un agente con Bash sin
restricciones podría rodear una regla de permisos), pero combinado con el
deny de ~/.secrets/ sube bastante el listón. Permite además lanzar el
servidor como stdio sin tener que meter el token en la config de MCP de
Claude Code.
"""
import base64
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_FILE = Path(os.environ.get("JIRA_GATEWAY_ENV_FILE", "~/.secrets/jira-gateway.env")).expanduser()
load_dotenv(_ENV_FILE)  # no-op silencioso si el fichero no existe


class ConfigError(Exception):
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ConfigError(f"falta la variable de entorno {name} (¿existe {_ENV_FILE}?)")
    return val


@dataclass
class Config:
    jira_email: str
    jira_api_token: str
    jira_cloud_id: str
    jira_site_url: str
    project_key: str
    in_progress_status: str
    selected_status: str
    default_issue_type: str
    subtask_issue_type: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            jira_email=_require("JIRA_EMAIL"),
            jira_api_token=_require("JIRA_API_TOKEN"),
            jira_cloud_id=_require("JIRA_CLOUD_ID"),
            jira_site_url=_require("JIRA_SITE_URL"),
            project_key=_require("JIRA_PROJECT_KEY"),
            in_progress_status=os.environ.get("JIRA_IN_PROGRESS_STATUS", "In Progress"),
            selected_status=os.environ.get("JIRA_SELECTED_STATUS", "Selected for Development"),
            default_issue_type=os.environ.get("JIRA_DEFAULT_ISSUE_TYPE", "Task"),
            subtask_issue_type=os.environ.get("JIRA_SUBTASK_ISSUE_TYPE", "Subtask"),
        )

    @property
    def jira_api_base(self) -> str:
        return f"https://api.atlassian.com/ex/jira/{self.jira_cloud_id}"

    @property
    def auth_header(self) -> str:
        raw = f"{self.jira_email}:{self.jira_api_token}".encode()
        return "Basic " + base64.b64encode(raw).decode()
