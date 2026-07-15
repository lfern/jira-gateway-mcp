"""
Todo lo que este módulo NO expone, el agente no puede hacerlo.
No hay ningún método "raw request" ni JQL libre.
"""
import re

import requests

from .config import Config

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


class JiraError(Exception):
    pass


def validate_issue_key(key: str, expected_project: str) -> None:
    if not _ISSUE_KEY_RE.match(key):
        raise JiraError(f"issue key con formato inválido: {key!r}")
    if not key.startswith(f"{expected_project}-"):
        raise JiraError(f"{key} no pertenece al proyecto {expected_project}")


class JiraClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._account_id: str | None = None

    def _headers(self) -> dict:
        return {"Authorization": self.cfg.auth_header, "Content-Type": "application/json"}

    def _current_account_id(self) -> str:
        if self._account_id is None:
            resp = requests.get(
                f"{self.cfg.jira_api_base}/rest/api/3/myself",
                headers=self._headers(),
                timeout=15,
            )
            if not resp.ok:
                raise JiraError(f"no pude identificar al usuario actual: {resp.status_code}")
            self._account_id = resp.json()["accountId"]
        return self._account_id

    def assert_assigned_to_me(self, issue_key: str) -> None:
        """Lanza JiraError si el issue no está asignado al usuario del token."""
        resp = requests.get(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}",
            headers=self._headers(),
            params={"fields": "assignee"},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"no pude leer {issue_key}: {resp.status_code}")

        assignee = resp.json()["fields"].get("assignee")
        if assignee is None or assignee.get("accountId") != self._current_account_id():
            who = assignee["displayName"] if assignee else "nadie (sin asignar)"
            raise JiraError(
                f"{issue_key} no está asignada a ti (asignada a: {who}). "
                "Asígnatela en Jira antes de empezarla."
            )

    def create_issue(self, summary: str, description: str, issue_type: str, labels: list[str] | None = None) -> dict:
        """Crea un issue. Requiere que el llamante ya haya confirmado —
        esta función no pregunta nada, solo ejecuta."""
        # La API v3 exige el campo description en formato ADF (Atlassian
        # Document Format), no texto plano.
        adf_description = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description}] if description else [],
                }
            ],
        }
        payload = {
            "fields": {
                "project": {"key": self.cfg.project_key},
                "summary": summary,
                "description": adf_description,
                "issuetype": {"name": issue_type},
                "labels": labels or [],
            }
        }
        resp = requests.post(
            f"{self.cfg.jira_api_base}/rest/api/3/issue",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al crear issue: {resp.text}")

        body = resp.json()
        key = body["key"]
        return {"key": key, "url": f"{self.cfg.jira_site_url}/browse/{key}"}

    def list_my_tasks(self) -> list[dict]:
        """Única query permitida: mis tareas abiertas en el proyecto configurado."""
        jql = (
            f"project = {self.cfg.project_key} AND assignee = currentUser() "
            "AND statusCategory != Done ORDER BY updated DESC"
        )
        resp = requests.get(
            f"{self.cfg.jira_api_base}/rest/api/3/search/jql",
            headers=self._headers(),
            params={"jql": jql, "fields": "summary,status"},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code}: {resp.text}")

        issues = resp.json().get("issues", [])
        return [
            {
                "key": i["key"],
                "summary": i["fields"]["summary"],
                "status": i["fields"]["status"]["name"],
            }
            for i in issues
        ]

    def transition_issue(self, issue_key: str, target_status: str) -> None:
        validate_issue_key(issue_key, self.cfg.project_key)

        url = f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/transitions"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if not resp.ok:
            raise JiraError(f"no pude leer transiciones de {issue_key}: {resp.status_code}")

        transitions = resp.json().get("transitions", [])
        target = next(
            (
                t
                for t in transitions
                if t["name"].lower() == target_status.lower()
            ),
            None,
        )
        if target is None:
            available = [t["name"] for t in transitions]
            raise JiraError(
                f"no encuentro la transición '{target_status}' "
                f"para {issue_key}. Disponibles: {available}"
            )

        resp = requests.post(
            url,
            headers=self._headers(),
            json={"transition": {"id": target["id"]}},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"fallo al transicionar {issue_key}: {resp.status_code}")
