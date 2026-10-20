"""
Todo lo que este módulo NO expone, el agente no puede hacerlo.
No hay ningún método "raw request" ni JQL libre.
"""
import re

import requests

from .adf import adf_to_markdown, markdown_to_adf
from .config import Config

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

# Nombre de estado que puede ir dentro de la JQL fija: letras (con tildes),
# dígitos, espacios y poca puntuación. Sin comillas ni barras invertidas, así
# que entre comillas no puede romper la query.
_STATUS_NAME_RE = re.compile(r"^[\w][\w .()/-]{0,59}$")

# Las etiquetas de Jira no admiten espacios.
_LABEL_RE = re.compile(r"^[^\s]{1,255}$")


class JiraError(Exception):
    pass


def validate_issue_key(key: str, expected_project: str) -> None:
    if not _ISSUE_KEY_RE.match(key):
        raise JiraError(f"issue key con formato inválido: {key!r}")
    if not key.startswith(f"{expected_project}-"):
        raise JiraError(f"{key} no pertenece al proyecto {expected_project}")


def validate_status_name(name: str) -> None:
    if not isinstance(name, str) or not _STATUS_NAME_RE.match(name):
        raise JiraError(f"nombre de estado con formato inválido: {name!r}")


def validate_labels(labels: list[str]) -> None:
    for label in labels:
        if not isinstance(label, str) or not _LABEL_RE.match(label):
            raise JiraError(f"etiqueta inválida: {label!r} (sin espacios, 1-255 caracteres)")


def _transition_destination(transition: dict) -> str:
    """Nombre del estado al que lleva una transición (Jira lo manda en `to`)."""
    return (transition.get("to") or {}).get("name") or transition["name"]


def validate_target_status(status: str, cfg: Config) -> None:
    """Aplica JIRA_ALLOWED_STATUSES si está configurada. Los estados que fija
    quien despliega (in_progress/selected) se permiten siempre."""
    if not cfg.jira_allowed_statuses:
        return
    allowed = {s.lower() for s in (*cfg.jira_allowed_statuses, cfg.in_progress_status, cfg.selected_status)}
    if status.lower() not in allowed:
        raise JiraError(
            f"estado no permitido: {status!r}. Permitidos: "
            f"{sorted({*cfg.jira_allowed_statuses, cfg.in_progress_status, cfg.selected_status})}"
        )


def _text_to_adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}] if text else [],
            }
        ],
    }


def _adf_to_text(node: dict) -> str:
    """Extrae texto plano de un documento ADF. Suficiente para mostrar
    comentarios al agente; no intenta preservar formato (listas, menciones,
    enlaces, etc. se aplanan a su texto)."""
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "mention":
        return node.get("attrs", {}).get("text", "")

    parts = [_adf_to_text(child) for child in node.get("content", [])]
    text = "".join(parts)
    if node.get("type") == "paragraph":
        text += "\n"
    return text


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

    def _get_issue_fields(self, issue_key: str, fields: list[str]) -> dict:
        """Lee los campos indicados de un issue del proyecto configurado.

        Además de validar el formato de la clave, comprueba el proyecto en la
        respuesta: Jira sigue redirigiendo claves antiguas de issues movidos a
        otro proyecto (PROJ-1 -> OTRO-7), así que el prefijo no basta."""
        validate_issue_key(issue_key, self.cfg.project_key)
        resp = requests.get(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}",
            headers=self._headers(),
            params={"fields": ",".join([*fields, "project"])},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"no pude leer {issue_key}: {resp.status_code}")

        body = resp.json()
        project = (body["fields"].get("project") or {}).get("key")
        if project != self.cfg.project_key:
            raise JiraError(
                f"{issue_key} se ha movido a {body.get('key')} (proyecto {project}), "
                f"fuera del proyecto {self.cfg.project_key}"
            )
        if body.get("key", issue_key) != issue_key:
            raise JiraError(f"{issue_key} ahora es {body['key']}: usa la clave actual")
        return body["fields"]

    def assert_assigned_to_me(self, issue_key: str) -> None:
        """Lanza JiraError si el issue no está asignado al usuario del token."""
        assignee = self._get_issue_fields(issue_key, ["assignee"]).get("assignee")
        if assignee is None or assignee.get("accountId") != self._current_account_id():
            who = assignee["displayName"] if assignee else "nadie (sin asignar)"
            raise JiraError(
                f"{issue_key} no está asignada a ti (asignada a: {who}). "
                "Asígnatela antes de empezarla (assign_task si está sin asignar)."
            )

    def assign_to_me(self, issue_key: str) -> dict:
        """Asigna el issue al usuario del token, y SOLO si está sin asignar.
        No acepta otro destinatario a propósito: no hay forma de asignar a
        terceros ni de quitarle una tarea a quien ya la tiene.

        Límite conocido: Jira no tiene asignación condicional, así que entre
        la lectura y el PUT queda una ventana (de milisegundos) en la que, si
        otra persona se la asigna justo entonces, el PUT la pisaría. Releer
        después no lo detecta (ya saldrías tú), así que no se intenta."""
        assignee = self._get_issue_fields(issue_key, ["assignee"]).get("assignee")
        me = self._current_account_id()
        if assignee is not None:
            if assignee.get("accountId") == me:
                return {"issue_key": issue_key, "already_assigned": True}
            raise JiraError(
                f"{issue_key} ya está asignada a {assignee.get('displayName')}. "
                "Solo puedo asignarte tareas que estén sin asignar."
            )

        resp = requests.put(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/assignee",
            headers=self._headers(),
            json={"accountId": me},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al asignar {issue_key}: {resp.text}")
        return {"issue_key": issue_key, "assigned": True}

    def get_issue(self, issue_key: str) -> dict:
        fields = self._get_issue_fields(
            issue_key, ["summary", "description", "status", "assignee", "labels", "parent"]
        )
        description, unsupported = adf_to_markdown(fields.get("description"))
        assignee = fields.get("assignee")
        parent = fields.get("parent")
        return {
            "key": issue_key,
            "url": f"{self.cfg.jira_site_url}/browse/{issue_key}",
            "summary": fields.get("summary", ""),
            "description": description,
            "description_unsupported": unsupported,
            "status": (fields.get("status") or {}).get("name"),
            "assignee": assignee.get("displayName") if assignee else None,
            "labels": fields.get("labels") or [],
            "parent": (
                {"key": parent["key"], "summary": parent.get("fields", {}).get("summary")}
                if parent
                else None
            ),
        }

    def update_issue(
        self,
        issue_key: str,
        summary: str | None = None,
        description: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> None:
        """Edita resumen y/o descripción (markdown) y añade/quita etiquetas
        sin tocar las demás. Requiere que el llamante ya haya confirmado —
        esta función no pregunta nada, solo ejecuta."""
        validate_issue_key(issue_key, self.cfg.project_key)
        validate_labels([*(add_labels or []), *(remove_labels or [])])
        fields = {}
        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = markdown_to_adf(description)
        label_ops = [{"add": l} for l in add_labels or []] + [{"remove": l} for l in remove_labels or []]
        if not fields and not label_ops:
            raise JiraError("nada que actualizar")

        payload = {}
        if fields:
            payload["fields"] = fields
        if label_ops:
            payload["update"] = {"labels": label_ops}
        resp = requests.put(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al actualizar {issue_key}: {resp.text}")

    def create_issue(
        self,
        summary: str,
        description: str,
        issue_type: str,
        labels: list[str] | None = None,
        parent_key: str | None = None,
    ) -> dict:
        """Crea un issue. Requiere que el llamante ya haya confirmado —
        esta función no pregunta nada, solo ejecuta."""
        validate_labels(labels or [])
        payload = {
            "fields": {
                "project": {"key": self.cfg.project_key},
                "summary": summary,
                "description": markdown_to_adf(description),
                "issuetype": {"name": issue_type},
                "labels": labels or [],
            }
        }
        if parent_key:
            payload["fields"]["parent"] = {"key": parent_key}
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

    def list_my_tasks(self, statuses: list[str] | None = None) -> list[dict]:
        """Mis tareas abiertas en el proyecto configurado, opcionalmente solo
        en los estados indicados."""
        return self._search_open("assignee = currentUser()", statuses)

    def list_unassigned_tasks(self, statuses: list[str] | None = None) -> list[dict]:
        """Tareas sin asignar y abiertas en el proyecto configurado,
        opcionalmente solo en los estados indicados."""
        return self._search_open("assignee IS EMPTY", statuses)

    def _search_open(self, assignee_clause: str, statuses: list[str] | None) -> list[dict]:
        """La JQL es fija; lo único que entra de fuera son nombres de estado
        ya validados (sin comillas), que van entre comillas en un `IN`."""
        status_clause = ""
        if statuses:
            for st in statuses:
                validate_status_name(st)
            status_clause = " AND status IN (" + ", ".join(f'"{st}"' for st in statuses) + ")"
        jql = (
            f"project = {self.cfg.project_key} AND {assignee_clause} "
            f"AND statusCategory != Done{status_clause} ORDER BY updated DESC"
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
        """`target_status` puede ser el nombre de la transición o el del
        estado al que lleva: en muchos workflows no coinciden (ej. la
        transición 'Devolver a X' lleva al estado 'X'). Si coincide con el
        nombre de una transición, gana esa. La allowlist se aplica al estado
        al que lleva de verdad, no al texto recibido."""
        validate_issue_key(issue_key, self.cfg.project_key)

        url = f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/transitions"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if not resp.ok:
            raise JiraError(f"no pude leer transiciones de {issue_key}: {resp.status_code}")

        transitions = resp.json().get("transitions", [])
        wanted = target_status.lower()
        target = next((t for t in transitions if t["name"].lower() == wanted), None) or next(
            (t for t in transitions if _transition_destination(t).lower() == wanted), None
        )
        if target is None:
            available = [f"{t['name']} → {_transition_destination(t)}" for t in transitions]
            raise JiraError(
                f"no encuentro la transición ni el estado '{target_status}' "
                f"para {issue_key}. Disponibles (transición → estado): {available}"
            )
        validate_target_status(_transition_destination(target), self.cfg)

        resp = requests.post(
            url,
            headers=self._headers(),
            json={"transition": {"id": target["id"]}},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"fallo al transicionar {issue_key}: {resp.status_code}")

    def get_comments(self, issue_key: str) -> list[dict]:
        validate_issue_key(issue_key, self.cfg.project_key)

        resp = requests.get(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/comment",
            headers=self._headers(),
            params={"orderBy": "created"},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"no pude leer comentarios de {issue_key}: {resp.status_code}")

        comments = resp.json().get("comments", [])
        return [
            {
                "author": c["author"]["displayName"],
                "created": c["created"],
                "body": _adf_to_text(c["body"]).strip(),
            }
            for c in comments
        ]

    def add_comment(self, issue_key: str, body: str) -> dict:
        """Añade un comentario. Requiere que el llamante ya haya confirmado —
        esta función no pregunta nada, solo ejecuta."""
        validate_issue_key(issue_key, self.cfg.project_key)

        resp = requests.post(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/comment",
            headers=self._headers(),
            json={"body": _text_to_adf(body)},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al comentar {issue_key}: {resp.text}")

        comment = resp.json()
        return {
            "id": comment["id"],
            "url": f"{self.cfg.jira_site_url}/browse/{issue_key}?focusedCommentId={comment['id']}",
        }
