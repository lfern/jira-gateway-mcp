"""
Todo lo que este módulo NO expone, el agente no puede hacerlo.
No hay ningún método "raw request" ni JQL libre.
"""
import re
from datetime import datetime, time

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

# Duración de un worklog: "2h", "45m", "1h 30m", "1.5h". Sin días ni
# semanas a propósito: en Jira dependen de la config del sitio (1d = 8h o
# no), y así el preview muestra exactamente lo que se guarda.
_DURATION_PART_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([hm])")
_DURATION_RE = re.compile(r"^\s*(?:\d+(?:[.,]\d+)?\s*[hm]\s*)+$")
# Tope por entrada para cazar errores tipo "30h" en vez de "3h".
MAX_WORKLOG_SECONDS = 12 * 3600

# Jira usa ids numéricos para los worklogs.
_WORKLOG_ID_RE = re.compile(r"^\d{1,20}$")

# Por defecto Jira resta lo apuntado de la estimación restante: igual que
# al apuntar horas desde la web, así que el equipo no nota diferencia.
_ADJUST_ESTIMATE = {"adjustEstimate": "auto"}


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


def parse_duration(text: str) -> int:
    """'1h 30m' -> 5400. Lanza JiraError si el formato no es válido, si da
    menos de un minuto o si pasa de MAX_WORKLOG_SECONDS."""
    if not isinstance(text, str) or not _DURATION_RE.match(text.lower()):
        raise JiraError(f"duración con formato inválido: {text!r} (usa '2h', '45m' o '1h 30m')")
    seconds = 0
    for amount, unit in _DURATION_PART_RE.findall(text.lower()):
        seconds += round(float(amount.replace(",", ".")) * (3600 if unit == "h" else 60))
    if seconds < 60:
        raise JiraError(f"duración demasiado corta: {text!r} (mínimo 1m)")
    if seconds > MAX_WORKLOG_SECONDS:
        raise JiraError(
            f"duración demasiado larga: {text!r} (máximo {format_duration(MAX_WORKLOG_SECONDS)} por entrada)"
        )
    return seconds


def format_duration(seconds: int) -> str:
    """5400 -> '1h 30m'."""
    hours, minutes = divmod(round(seconds / 60), 60)
    return " ".join(part for part in (f"{hours}h" if hours else "", f"{minutes}m" if minutes else "") if part) or "0m"


def parse_started(text: str | None, now: datetime | None = None) -> str:
    """Fecha de inicio de un worklog en el formato que pide Jira. Acepta
    'YYYY-MM-DD' (se toma a las 09:00) o 'YYYY-MM-DDTHH:MM', en hora local
    si no trae zona. Sin valor, ahora. No admite fechas futuras."""
    now = now or datetime.now().astimezone()
    if text is None:
        started = now
    else:
        try:
            started = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            raise JiraError(f"fecha con formato inválido: {text!r} (usa 'YYYY-MM-DD' o 'YYYY-MM-DDTHH:MM')")
        if len(text) == 10:
            started = datetime.combine(started.date(), time(9, 0))
        if started.tzinfo is None:
            started = started.replace(tzinfo=now.tzinfo)
        if started > now:
            raise JiraError(f"la fecha no puede ser futura: {text!r}")
    return started.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def validate_worklog_id(worklog_id: str) -> str:
    worklog_id = str(worklog_id)
    if not _WORKLOG_ID_RE.match(worklog_id):
        raise JiraError(f"worklog_id inválido: {worklog_id!r}")
    return worklog_id


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


def _compact_attachment(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "filename": a.get("filename"),
        "size": a.get("size"),
        "mime_type": a.get("mimeType"),
        "author": (a.get("author") or {}).get("displayName"),
        "created": a.get("created"),
    }


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
            issue_key, ["summary", "description", "status", "assignee", "labels", "parent", "attachment"]
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
            "attachments": [_compact_attachment(a) for a in fields.get("attachment") or []],
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
            json={"body": markdown_to_adf(body)},
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al comentar {issue_key}: {resp.text}")

        comment = resp.json()
        return {
            "id": comment["id"],
            "url": f"{self.cfg.jira_site_url}/browse/{issue_key}?focusedCommentId={comment['id']}",
        }

    # --- worklogs (tiempo dedicado) -------------------------------------------
    #
    # Se puede apuntar tiempo en cualquier issue del proyecto, pero solo se
    # pueden editar o borrar las entradas propias: la autoría se comprueba
    # contra el usuario del token antes de tocar nada.

    def _compact_worklog(self, w: dict) -> dict:
        author = w.get("author") or {}
        return {
            "id": w["id"],
            "author": author.get("displayName"),
            "mine": author.get("accountId") == self._current_account_id(),
            "started": w.get("started"),
            "time_spent": format_duration(w.get("timeSpentSeconds", 0)),
            "time_spent_seconds": w.get("timeSpentSeconds", 0),
            "comment": _adf_to_text(w["comment"]).strip() if w.get("comment") else "",
        }

    def list_worklogs(self, issue_key: str) -> list[dict]:
        self._get_issue_fields(issue_key, [])
        resp = requests.get(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/worklog",
            headers=self._headers(),
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"no pude leer el tiempo apuntado en {issue_key}: {resp.status_code}")
        return [self._compact_worklog(w) for w in resp.json().get("worklogs", [])]

    def get_my_worklog(self, issue_key: str, worklog_id: str) -> dict:
        """Lee un worklog y lanza JiraError si no es del usuario del token."""
        worklog_id = validate_worklog_id(worklog_id)
        self._get_issue_fields(issue_key, [])
        resp = requests.get(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
            headers=self._headers(),
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"no pude leer el worklog {worklog_id} de {issue_key}: {resp.status_code}")
        worklog = self._compact_worklog(resp.json())
        if not worklog["mine"]:
            raise JiraError(
                f"el worklog {worklog_id} de {issue_key} es de {worklog['author']}. "
                "Solo puedes editar o borrar tus propias entradas."
            )
        return worklog

    def add_worklog(self, issue_key: str, seconds: int, started: str, comment: str = "") -> dict:
        """Apunta tiempo. Requiere que el llamante ya haya confirmado —
        esta función no pregunta nada, solo ejecuta."""
        self._get_issue_fields(issue_key, [])
        payload = {"timeSpentSeconds": seconds, "started": started}
        if comment:
            payload["comment"] = _text_to_adf(comment)
        resp = requests.post(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/worklog",
            headers=self._headers(),
            params=_ADJUST_ESTIMATE,
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al apuntar tiempo en {issue_key}: {resp.text}")
        return {"id": resp.json()["id"]}

    def update_worklog(
        self,
        issue_key: str,
        worklog_id: str,
        seconds: int | None = None,
        started: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Corrige un worklog propio. Requiere que el llamante ya haya
        confirmado — esta función no pregunta nada, solo ejecuta."""
        worklog_id = validate_worklog_id(worklog_id)
        self.get_my_worklog(issue_key, worklog_id)
        payload = {}
        if seconds is not None:
            payload["timeSpentSeconds"] = seconds
        if started is not None:
            payload["started"] = started
        if comment is not None:
            payload["comment"] = _text_to_adf(comment)
        if not payload:
            raise JiraError("nada que actualizar")
        resp = requests.put(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
            headers=self._headers(),
            params=_ADJUST_ESTIMATE,
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al corregir el worklog {worklog_id}: {resp.text}")

    def delete_worklog(self, issue_key: str, worklog_id: str) -> None:
        """Borra un worklog propio. Requiere que el llamante ya haya
        confirmado — esta función no pregunta nada, solo ejecuta."""
        worklog_id = validate_worklog_id(worklog_id)
        self.get_my_worklog(issue_key, worklog_id)
        resp = requests.delete(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
            headers=self._headers(),
            params=_ADJUST_ESTIMATE,
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al borrar el worklog {worklog_id}: {resp.text}")

    # --- adjuntos ----------------------------------------------------------------

    def add_attachments(self, issue_key: str, files: list[tuple[str, bytes, str]]) -> list[dict]:
        """Sube ficheros (nombre, contenido, tipo MIME) a un issue. Requiere
        que el llamante ya haya confirmado — esta función no pregunta nada,
        solo ejecuta. No lee del disco: el contenido lo da el llamante."""
        self._get_issue_fields(issue_key, [])

        resp = requests.get(
            f"{self.cfg.jira_api_base}/rest/api/3/attachment/meta",
            headers=self._headers(),
            timeout=15,
        )
        if not resp.ok:
            raise JiraError(f"no pude leer la config de adjuntos de Jira: {resp.status_code}")
        meta = resp.json()
        if not meta.get("enabled"):
            raise JiraError("los adjuntos están desactivados en este Jira")
        limit = meta.get("uploadLimit")
        for name, content, _ in files:
            if limit and len(content) > limit:
                raise JiraError(f"{name} ocupa {len(content)} bytes, más que el límite de Jira ({limit})")

        # Sin Content-Type: requests lo pone con el boundary del multipart.
        # X-Atlassian-Token es obligatoria en este endpoint (protección XSRF).
        resp = requests.post(
            f"{self.cfg.jira_api_base}/rest/api/3/issue/{issue_key}/attachments",
            headers={
                "Authorization": self.cfg.auth_header,
                "Accept": "application/json",
                "X-Atlassian-Token": "no-check",
            },
            files=[("file", (name, content, mime)) for name, content, mime in files],
            timeout=60,
        )
        if not resp.ok:
            raise JiraError(f"Jira {resp.status_code} al adjuntar en {issue_key}: {resp.text}")
        return [_compact_attachment(a) for a in resp.json()]
