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
