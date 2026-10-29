import importlib
import json

import pytest
import responses

from gateway.jira_client import JiraClient, JiraError
from tests.conftest import make_config

BASE = "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3"
ME = "acc-me"


def _issue(key="PROJ-7", project="PROJ", assignee=None, description=None, summary="Resumen"):
    return {
        "key": key,
        "fields": {
            "project": {"key": project},
            "summary": summary,
            "description": description,
            "status": {"name": "To Do"},
            "assignee": assignee,
            "labels": ["backend"],
            "parent": {"key": "PROJ-1", "fields": {"summary": "Épica"}},
            "attachment": [
                {
                    "id": "10001",
                    "filename": "captura.png",
                    "size": 2048,
                    "mimeType": "image/png",
                    "author": {"displayName": "Ana"},
                    "created": "2026-10-29T10:00:00.000+0100",
                    "content": "https://example.atlassian.net/rest/api/3/attachment/content/10001",
                }
            ],
        },
    }


def _add_issue(**kwargs):
    key = kwargs.get("key", "PROJ-7")
    responses.add(responses.GET, f"{BASE}/issue/{key}", json=_issue(**kwargs))


def _add_me():
    responses.add(responses.GET, f"{BASE}/myself", json={"accountId": ME})


@pytest.fixture
def client(cfg):
    return JiraClient(cfg)


# --- get_issue -----------------------------------------------------------------


@responses.activate
def test_get_issue_returns_markdown_and_fields(client):
    description = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hola", "marks": [{"type": "strong"}]}]}],
    }
    _add_issue(description=description, assignee={"accountId": "x", "displayName": "Ana"})

    assert client.get_issue("PROJ-7") == {
        "key": "PROJ-7",
        "url": "https://example.atlassian.net/browse/PROJ-7",
        "summary": "Resumen",
        "description": "**hola**",
        "description_unsupported": [],
        "status": "To Do",
        "assignee": "Ana",
        "labels": ["backend"],
        "parent": {"key": "PROJ-1", "summary": "Épica"},
        "attachments": [
            {
                "id": "10001",
                "filename": "captura.png",
                "size": 2048,
                "mime_type": "image/png",
                "author": "Ana",
                "created": "2026-10-29T10:00:00.000+0100",
            }
        ],
    }


def test_get_issue_rejects_other_project_without_calling_jira(client):
    with pytest.raises(JiraError, match="no pertenece"):
        client.get_issue("OTRO-1")


@responses.activate
def test_get_issue_rejects_issue_moved_to_other_project(client):
    # Jira redirige la clave antigua al issue movido.
    responses.add(responses.GET, f"{BASE}/issue/PROJ-7", json=_issue(key="OTRO-3", project="OTRO"))
    with pytest.raises(JiraError, match="se ha movido"):
        client.get_issue("PROJ-7")


@responses.activate
def test_get_issue_rejects_renamed_key_in_same_project(client):
    responses.add(responses.GET, f"{BASE}/issue/PROJ-7", json=_issue(key="PROJ-9"))
    with pytest.raises(JiraError, match="ahora es PROJ-9"):
        client.get_issue("PROJ-7")


# --- assign_to_me --------------------------------------------------------------


@responses.activate
def test_assign_to_me_assigns_unassigned_issue_to_token_user(client):
    _add_issue(assignee=None)
    _add_me()
    responses.add(responses.PUT, f"{BASE}/issue/PROJ-7/assignee", status=204)

    assert client.assign_to_me("PROJ-7") == {"issue_key": "PROJ-7", "assigned": True}
    assert json.loads(responses.calls[-1].request.body) == {"accountId": ME}


@responses.activate
def test_assign_to_me_refuses_issue_assigned_to_someone_else(client):
    _add_issue(assignee={"accountId": "acc-ana", "displayName": "Ana"})
    _add_me()

    with pytest.raises(JiraError, match="ya está asignada a Ana"):
        client.assign_to_me("PROJ-7")
    assert not any(c.request.method == "PUT" for c in responses.calls)


@responses.activate
def test_assign_to_me_is_noop_when_already_mine(client):
    _add_issue(assignee={"accountId": ME, "displayName": "Yo"})
    _add_me()

    assert client.assign_to_me("PROJ-7") == {"issue_key": "PROJ-7", "already_assigned": True}
    assert not any(c.request.method == "PUT" for c in responses.calls)


def test_assign_to_me_rejects_other_project(client):
    with pytest.raises(JiraError, match="no pertenece"):
        client.assign_to_me("OTRO-1")


# --- update_issue ---------------------------------------------------------------


@responses.activate
def test_update_issue_sends_only_given_fields_as_adf(client):
    responses.add(responses.PUT, f"{BASE}/issue/PROJ-7", status=204)

    client.update_issue("PROJ-7", description="- a")

    sent = json.loads(responses.calls[0].request.body)
    assert list(sent["fields"]) == ["description"]
    assert sent["fields"]["description"]["content"][0]["type"] == "bulletList"


def test_update_issue_rejects_other_project(client):
    with pytest.raises(JiraError, match="no pertenece"):
        client.update_issue("OTRO-1", summary="x")


# --- tools del servidor ----------------------------------------------------------


@pytest.fixture
def server(monkeypatch):
    for name, value in {
        "JIRA_EMAIL": "jira@example.com",
        "JIRA_API_TOKEN": "t",
        "JIRA_CLOUD_ID": "cloud-id",
        "JIRA_SITE_URL": "https://example.atlassian.net",
        "JIRA_PROJECT_KEY": "PROJ",
    }.items():
        monkeypatch.setenv(name, value)
    import gateway.server

    module = importlib.reload(gateway.server)
    monkeypatch.setattr(module, "_jira", JiraClient(make_config()))
    return module


@responses.activate
def test_update_task_preview_shows_diff_and_sends_nothing(server):
    _add_issue(description=None, summary="Viejo")

    result = server.update_task("PROJ-7", summary="Nuevo", description="línea nueva")

    assert result["preview"] is True
    assert result["summary"] == {"before": "Viejo", "after": "Nuevo"}
    assert "+línea nueva" in result["description_diff"]
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_update_task_confirm_sends_changes(server):
    _add_issue(summary="Viejo")
    responses.add(responses.PUT, f"{BASE}/issue/PROJ-7", status=204)

    result = server.update_task("PROJ-7", summary="Nuevo", confirm=True)

    assert result["updated"] is True
    assert json.loads(responses.calls[-1].request.body) == {"fields": {"summary": "Nuevo"}}


@responses.activate
def test_update_task_without_changes_sends_nothing(server):
    _add_issue(summary="Igual")

    result = server.update_task("PROJ-7", summary="Igual", confirm=True)

    assert result["no_changes"] is True
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_update_task_warns_when_description_would_lose_elements(server):
    mention = {"type": "mention", "attrs": {"id": "1", "text": "@Ana"}}
    _add_issue(description={"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [mention]}]})

    result = server.update_task("PROJ-7", description="otra cosa")

    assert "mention" in result["description_warning"]


def test_update_task_requires_something_to_change(server):
    assert "error" in server.update_task("PROJ-7")


@responses.activate
def test_get_task_tool_maps_unsupported_to_warning(server):
    mention = {"type": "mention", "attrs": {"id": "1", "text": "@Ana"}}
    _add_issue(description={"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [mention]}]})

    result = server.get_task("PROJ-7")

    assert "description_unsupported" not in result
    assert "mention" in result["description_warning"]
    assert result["description"] == "@Ana"


@responses.activate
def test_get_task_tool_without_unsupported_has_no_warning(server):
    _add_issue()
    result = server.get_task("PROJ-7")
    assert "description_warning" not in result
    assert "description_unsupported" not in result


def test_get_task_tool_wraps_errors(server):
    assert "no pertenece" in server.get_task("OTRO-1")["error"]


@responses.activate
def test_assign_task_tool_success(server):
    _add_issue(assignee=None)
    _add_me()
    responses.add(responses.PUT, f"{BASE}/issue/PROJ-7/assignee", status=204)
    assert server.assign_task("PROJ-7") == {"issue_key": "PROJ-7", "assigned": True}


@responses.activate
def test_assign_task_tool_wraps_refusal(server):
    _add_issue(assignee={"accountId": "acc-ana", "displayName": "Ana"})
    _add_me()
    assert "ya está asignada a Ana" in server.assign_task("PROJ-7")["error"]


# --- filtro por estado -----------------------------------------------------------


@responses.activate
def test_list_my_tasks_adds_quoted_status_filter(client):
    responses.add(responses.GET, f"{BASE}/search/jql", json={"issues": []})

    client.list_my_tasks(["Selected for Development", "Listo para validar"])

    jql = responses.calls[0].request.params["jql"]
    assert jql.startswith("project = PROJ AND assignee = currentUser() AND statusCategory != Done")
    assert 'status IN ("Selected for Development", "Listo para validar")' in jql


@responses.activate
def test_list_unassigned_tasks_without_statuses_keeps_fixed_jql(client):
    responses.add(responses.GET, f"{BASE}/search/jql", json={"issues": []})

    client.list_unassigned_tasks()

    assert responses.calls[0].request.params["jql"] == (
        "project = PROJ AND assignee IS EMPTY AND statusCategory != Done ORDER BY updated DESC"
    )


@pytest.mark.parametrize(
    "bad",
    ['Done" OR project = OTRO OR status = "x', "a\\b", "", "x' OR 1=1", "a\nb"],
)
def test_list_my_tasks_rejects_injection_without_calling_jira(client, bad):
    with pytest.raises(JiraError, match="formato inválido"):
        client.list_my_tasks([bad])


# --- transiciones y allowlist de estados -----------------------------------------


def _add_transitions(key="PROJ-7"):
    url = f"{BASE}/issue/{key}/transitions"
    responses.add(
        responses.GET,
        url,
        json={
            "transitions": [
                {"id": "31", "name": "Listo para validar", "to": {"name": "Listo para validar"}},
                {"id": "41", "name": "Done", "to": {"name": "Done"}},
                {"id": "51", "name": "Devolver a In Progress", "to": {"name": "In Progress"}},
                # Nombre engañoso: suena a un estado permitido pero lleva a Done.
                {"id": "61", "name": "Revisar", "to": {"name": "Done"}},
            ]
        },
    )
    responses.add(responses.POST, url, status=204)


@responses.activate
def test_transition_task_moves_my_issue(server):
    _add_issue(assignee={"accountId": ME, "displayName": "Yo"})
    _add_me()
    _add_transitions()

    assert server.transition_task("PROJ-7", "listo para validar") == {
        "issue_key": "PROJ-7",
        "status": "listo para validar",
    }
    assert json.loads(responses.calls[-1].request.body) == {"transition": {"id": "31"}}


@responses.activate
def test_transition_task_refuses_issue_of_someone_else(server):
    _add_issue(assignee={"accountId": "acc-ana", "displayName": "Ana"})
    _add_me()

    assert "no está asignada a ti" in server.transition_task("PROJ-7", "Done")["error"]
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_transition_issue_respects_allowed_statuses():
    client = JiraClient(make_config(jira_allowed_statuses=("Listo para validar",)))
    _add_transitions()

    with pytest.raises(JiraError, match="estado no permitido"):
        client.transition_issue("PROJ-7", "Done")
    # La allowlist mira el estado destino, no el nombre de la transición.
    client_revisar = JiraClient(make_config(jira_allowed_statuses=("Revisar",)))
    with pytest.raises(JiraError, match="estado no permitido"):
        client_revisar.transition_issue("PROJ-7", "Revisar")
    assert all(c.request.method == "GET" for c in responses.calls)

    client.transition_issue("PROJ-7", "LISTO PARA VALIDAR")


@responses.activate
def test_transition_issue_accepts_destination_status_name(client):
    _add_transitions()

    client.transition_issue("PROJ-7", "in progress")

    assert json.loads(responses.calls[-1].request.body) == {"transition": {"id": "51"}}


@responses.activate
def test_transition_issue_unknown_lists_transitions_and_destinations(client):
    _add_transitions()

    with pytest.raises(JiraError, match="Devolver a In Progress → In Progress"):
        client.transition_issue("PROJ-7", "No existe")


def test_allowed_statuses_always_include_configured_ones():
    from gateway.jira_client import validate_target_status

    cfg = make_config(jira_allowed_statuses=("Listo para validar",))
    validate_target_status("in progress", cfg)
    validate_target_status("Selected for Development", cfg)


# --- etiquetas -------------------------------------------------------------------


@responses.activate
def test_update_task_rejects_same_label_in_add_and_remove(server):
    _add_issue()  # labels actuales: ["backend"]

    result = server.update_task("PROJ-7", add_labels=["EXT", "backend"], remove_labels=["backend"])

    assert "add_labels y remove_labels" in result["error"]
    assert len(responses.calls) == 0


@responses.activate
def test_update_task_labels_preview_and_confirm(server):
    _add_issue()  # labels actuales: ["backend"]
    responses.add(responses.PUT, f"{BASE}/issue/PROJ-7", status=204)

    preview = server.update_task("PROJ-7", add_labels=["EXT", "backend"], remove_labels=["frontend"])
    assert preview["preview"] is True
    assert preview["labels"] == {"before": ["backend"], "after": ["backend", "EXT"]}
    assert all(c.request.method == "GET" for c in responses.calls)

    result = server.update_task("PROJ-7", add_labels=["EXT"], remove_labels=["backend"], confirm=True)
    assert result["updated"] is True
    assert json.loads(responses.calls[-1].request.body) == {
        "update": {"labels": [{"add": "EXT"}, {"remove": "backend"}]}
    }


@responses.activate
def test_update_task_labels_already_applied_is_noop(server):
    _add_issue()

    result = server.update_task("PROJ-7", add_labels=["backend"], confirm=True)

    assert result["no_changes"] is True
    assert all(c.request.method == "GET" for c in responses.calls)


def test_update_task_rejects_label_with_spaces(server):
    assert "etiqueta inválida" in server.update_task("PROJ-7", add_labels=["con espacio"])["error"]
