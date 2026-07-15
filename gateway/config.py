"""
Config del gateway. Todo sale de variables de entorno — el agente (Claude Code)
nunca ve estos valores, solo ve las tools que exponemos en server.py.
"""
import base64
import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ConfigError(f"falta la variable de entorno {name}")
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
        )

    @property
    def jira_api_base(self) -> str:
        return f"https://api.atlassian.com/ex/jira/{self.jira_cloud_id}"

    @property
    def auth_header(self) -> str:
        raw = f"{self.jira_email}:{self.jira_api_token}".encode()
        return "Basic " + base64.b64encode(raw).decode()
