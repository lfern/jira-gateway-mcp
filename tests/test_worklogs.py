import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import responses

from gateway.jira_client import JiraClient, JiraError, format_duration, parse_duration, parse_started
from tests.conftest import make_config

BASE = "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3"
ME = "acc-me"
CET = timezone(timedelta(hours=2))


def _add_issue(key="PROJ-7"):
    responses.add(responses.GET, f"{BASE}/issue/{key}", json={"key": key, "fields": {"project": {"key": "PROJ"}}})


def _add_me():
    responses.add(responses.GET, f"{BASE}/myself", json={"accountId": ME})


def _worklog(wid="100", account=ME, name="Yo", seconds=3600, comment=None):
    return {
        "id": wid,
        "author": {"accountId": account, "displayName": name},
        "started": "2026-10-19T09:00:00.000+0200",
        "timeSpentSeconds": seconds,
        "comment": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}]}
        if comment
        else None,
    }


def _add_worklog(**kwargs):
    wid = kwargs.get("wid", "100")
    responses.add(responses.GET, f"{BASE}/issue/PROJ-7/worklog/{wid}", json=_worklog(**kwargs))


@pytest.fixture
def client(cfg):
    return JiraClient(cfg)


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


def _writes():
    return [c for c in responses.calls if c.request.method != "GET"]


# --- duración y fecha --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [("2h", 7200), ("45m", 2700), ("1h 30m", 5400), ("1h30m", 5400), ("1.5h", 5400), ("1,5h", 5400), ("12h", 43200)],
)
def test_parse_duration_accepts_hours_and_minutes(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "2", "1d", "1w", "2 horas", "-1h", "13h", "0m", "30s", "1h; DROP"])
def test_parse_duration_rejects_bad_values(bad):
    with pytest.raises(JiraError):
        parse_duration(bad)


def test_format_duration():
    assert format_duration(5400) == "1h 30m"
    assert format_duration(7200) == "2h"
    assert format_duration(2700) == "45m"


def test_parse_started_formats():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=CET)
    assert parse_started(None, now) == "2026-10-20T12:00:00.000+0200"
    assert parse_started("2026-10-19", now) == "2026-10-19T09:00:00.000+0200"
    assert parse_started("2026-10-19T15:30", now) == "2026-10-19T15:30:00.000+0200"


@pytest.mark.parametrize("bad", ["2026-10-21", "ayer", "19/10/2026"])
def test_parse_started_rejects_future_and_bad_format(bad):
    with pytest.raises(JiraError):
        parse_started(bad, datetime(2026, 10, 20, 12, 0, tzinfo=CET))


# --- cliente ---------------------------------------------------------------------


@responses.activate
def test_list_worklogs_marks_mine(client):
    _add_issue()
    _add_me()
    responses.add(
        responses.GET,
        f"{BASE}/issue/PROJ-7/worklog",
        json={"worklogs": [_worklog(comment="revisión"), _worklog(wid="101", account="acc-ana", name="Ana", seconds=1800)]},
    )

    result = client.list_worklogs("PROJ-7")

    assert result[0] == {
        "id": "100",
        "author": "Yo",
        "mine": True,
        "started": "2026-10-19T09:00:00.000+0200",
        "time_spent": "1h",
        "time_spent_seconds": 3600,
        "comment": "revisión",
    }
    assert result[1]["mine"] is False
    assert result[1]["time_spent"] == "30m"


def test_list_worklogs_rejects_other_project(client):
    with pytest.raises(JiraError, match="no pertenece"):
        client.list_worklogs("OTRO-1")


@responses.activate
def test_add_worklog_sends_seconds_started_and_auto_estimate(client):
    _add_issue()
    responses.add(responses.POST, f"{BASE}/issue/PROJ-7/worklog", json={"id": "200"})

    assert client.add_worklog("PROJ-7", 5400, "2026-10-19T09:00:00.000+0200", "pruebas") == {"id": "200"}

    request = _writes()[0].request
    assert request.params["adjustEstimate"] == "auto"
    body = json.loads(request.body)
    assert body["timeSpentSeconds"] == 5400
    assert body["started"] == "2026-10-19T09:00:00.000+0200"
    assert body["comment"]["content"][0]["content"][0]["text"] == "pruebas"


@responses.activate
def test_update_worklog_refuses_someone_elses_entry(client):
    _add_issue()
    _add_me()
    _add_worklog(account="acc-ana", name="Ana")

    with pytest.raises(JiraError, match="es de Ana"):
        client.update_worklog("PROJ-7", "100", seconds=60)
    assert _writes() == []


@responses.activate
def test_delete_worklog_refuses_someone_elses_entry(client):
    _add_issue()
    _add_me()
    _add_worklog(account="acc-ana", name="Ana")

    with pytest.raises(JiraError, match="es de Ana"):
        client.delete_worklog("PROJ-7", "100")
    assert _writes() == []


@pytest.mark.parametrize("bad", ["abc", "1/../2", "", "100?x=1"])
def test_worklog_id_is_validated(client, bad):
    with pytest.raises(JiraError, match="worklog_id inválido"):
        client.delete_worklog("PROJ-7", bad)


# --- tools del servidor ------------------------------------------------------------


@responses.activate
def test_add_worklog_tool_preview_sends_nothing(server):
    result = server.add_worklog("PROJ-7", "1h 30m", started="2026-10-19", comment="pruebas")

    assert result["preview"] is True
    assert result["time_spent"] == "1h 30m"
    assert result["started"].startswith("2026-10-19T09:00:00.000")
    assert len(responses.calls) == 0


def test_add_worklog_tool_rejects_too_long(server):
    assert "demasiado larga" in server.add_worklog("PROJ-7", "30h")["error"]


@responses.activate
def test_add_worklog_tool_confirm_creates(server):
    _add_issue()
    responses.add(responses.POST, f"{BASE}/issue/PROJ-7/worklog", json={"id": "200"})

    result = server.add_worklog("PROJ-7", "2h", started="2026-10-19", confirm=True)

    assert result == {"created": True, "issue_key": "PROJ-7", "id": "200", "time_spent": "2h"}


@responses.activate
def test_update_worklog_tool_preview_shows_before_after(server):
    _add_issue()
    _add_me()
    _add_worklog(seconds=3600)

    result = server.update_worklog("PROJ-7", "100", time_spent="1h 30m")

    assert result["preview"] is True
    assert result["time_spent"] == {"before": "1h", "after": "1h 30m"}
    assert _writes() == []


@responses.activate
def test_update_worklog_tool_confirm_sends_only_changes(server):
    _add_issue()
    _add_me()
    _add_worklog(seconds=3600)
    responses.add(responses.PUT, f"{BASE}/issue/PROJ-7/worklog/100", json={})

    result = server.update_worklog("PROJ-7", "100", time_spent="1h 30m", comment="", confirm=True)

    assert result["updated"] is True
    request = _writes()[0].request
    assert request.params["adjustEstimate"] == "auto"
    assert json.loads(request.body) == {"timeSpentSeconds": 5400}


@responses.activate
def test_update_worklog_tool_refuses_someone_elses_entry(server):
    _add_issue()
    _add_me()
    _add_worklog(account="acc-ana", name="Ana")

    assert "es de Ana" in server.update_worklog("PROJ-7", "100", time_spent="2h")["error"]


@responses.activate
def test_delete_worklog_tool_preview_then_confirm(server):
    _add_issue()
    _add_me()
    _add_worklog()
    responses.add(responses.DELETE, f"{BASE}/issue/PROJ-7/worklog/100", status=204)

    preview = server.delete_worklog("PROJ-7", "100")
    assert preview["preview"] is True
    assert preview["worklog"]["time_spent"] == "1h"
    assert _writes() == []

    result = server.delete_worklog("PROJ-7", "100", confirm=True)
    assert result["deleted"] is True
    assert _writes()[0].request.params["adjustEstimate"] == "auto"
