import importlib
import json

import pytest
import responses

from gateway.bitbucket_client import BitbucketClient, BitbucketError, normalize_reviewers
from tests.conftest import make_config
from tests.test_bitbucket_client import _pr_body

API = "https://api.bitbucket.org/2.0"
REPO = f"{API}/repositories/dividend-refund/refunder-react"
PRS = f"{REPO}/pullrequests"
MEMBERS = f"{API}/workspaces/dividend-refund/members"
DEFAULTS = f"{REPO}/effective-default-reviewers"
ME = "acc-yo"


def _user(account_id, name, nickname, uuid):
    return {"display_name": name, "account_id": account_id, "nickname": nickname, "uuid": uuid}


ANA = _user("acc-ana", "Ana García", "ana", "{11111111-aaaa}")
BEA = _user("acc-bea", "Bea López", "bea", "{22222222-bbbb}")
CARLOS = _user("acc-carlos", "Carlos Ruiz", "carlos", "{33333333-cccc}")
YO = _user(ME, "Luis", "luis", "{99999999-yyyy}")


def _add_members(*users):
    responses.add(responses.GET, MEMBERS, json={"values": [{"user": u} for u in users]})


def _add_me():
    responses.add(responses.GET, f"{API}/user", json=YO)


def _add_defaults(*users):
    responses.add(
        responses.GET,
        DEFAULTS,
        json={"values": [{"reviewer_type": "repository", "user": u} for u in users]},
    )


def _add_lookups(defaults=()):
    _add_members(ANA, BEA, CARLOS, YO)
    _add_me()
    _add_defaults(*defaults)


def _posts():
    return [c for c in responses.calls if c.request.method == "POST"]


# --- normalize_reviewers ------------------------------------------------------


def test_normalize_reviewers_strips_and_dedupes():
    assert normalize_reviewers([" ana ", "", "ana", "acc-bea"]) == ["ana", "acc-bea"]


def test_normalize_reviewers_rejects_email():
    with pytest.raises(BitbucketError, match="email"):
        normalize_reviewers(["ana@example.com"])


@pytest.mark.parametrize("bad", ["con espacio", "a/b", "x" * 129])
def test_normalize_reviewers_rejects_bad_format(bad):
    with pytest.raises(BitbucketError, match="formato"):
        normalize_reviewers([bad])


def test_normalize_reviewers_rejects_too_many():
    with pytest.raises(BitbucketError, match="demasiados"):
        normalize_reviewers([f"r{i}" for i in range(11)])


# --- resolve_reviewers ----------------------------------------------------------


@responses.activate
def test_resolve_accepts_account_id_uuid_and_nickname(cfg):
    _add_lookups()
    result = BitbucketClient(cfg).resolve_reviewers(
        "refunder-react", ["acc-ana", "22222222-BBBB", "Carlos"]
    )
    assert result["reviewers"] == [
        {"display_name": "Ana García", "account_id": "acc-ana"},
        {"display_name": "Bea López", "account_id": "acc-bea"},
        {"display_name": "Carlos Ruiz", "account_id": "acc-carlos"},
    ]
    assert result["account_ids"] == ["acc-ana", "acc-bea", "acc-carlos"]
    assert result["warnings"] == []


@responses.activate
def test_resolve_follows_member_pagination(cfg):
    responses.add(
        responses.GET, MEMBERS, json={"values": [{"user": ANA}], "next": f"{MEMBERS}?page=2"}
    )
    responses.add(responses.GET, f"{MEMBERS}?page=2", json={"values": [{"user": BEA}]})
    _add_me()
    _add_defaults()

    result = BitbucketClient(cfg).resolve_reviewers("refunder-react", ["bea"])

    assert result["account_ids"] == ["acc-bea"]


@responses.activate
def test_resolve_adds_defaults_without_duplicates_or_author(cfg):
    _add_lookups(defaults=(ANA, CARLOS, YO))
    result = BitbucketClient(cfg).resolve_reviewers("refunder-react", ["ana", "acc-ana", "bea"])
    assert [u["account_id"] for u in result["reviewers"]] == ["acc-ana", "acc-bea"]
    assert result["default_reviewers"] == [{"display_name": "Carlos Ruiz", "account_id": "acc-carlos"}]
    assert result["account_ids"] == ["acc-ana", "acc-bea", "acc-carlos"]


@responses.activate
def test_resolve_unknown_reviewer_names_it(cfg):
    _add_lookups()
    with pytest.raises(BitbucketError, match="nadie"):
        BitbucketClient(cfg).resolve_reviewers("refunder-react", ["ana", "nadie"])


@responses.activate
def test_resolve_members_403_gives_scope_hint(cfg):
    responses.add(responses.GET, MEMBERS, json={}, status=403)
    with pytest.raises(BitbucketError, match="read:workspace:bitbucket"):
        BitbucketClient(cfg).resolve_reviewers("refunder-react", ["ana"])


# --- tool create_pull_request / get_pull_request ----------------------------------


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
    _use_config(module, monkeypatch, make_config())
    return module


def _use_config(module, monkeypatch, cfg):
    monkeypatch.setattr(module, "_cfg", cfg)
    monkeypatch.setattr(module, "_bitbucket", BitbucketClient(cfg))


def _add_no_open_pr():
    responses.add(responses.GET, PRS, json={"values": []})


def _create(server, **kwargs):
    return server.create_pull_request("feature/REF-432-algo", "feat: algo", "texto", **kwargs)


@responses.activate
def test_tool_without_reviewers_sends_same_body_as_before(server):
    _add_no_open_pr()
    responses.add(responses.POST, PRS, json=_pr_body(), status=201)

    result = _create(server, confirm=True)

    assert result["created"] is True
    assert json.loads(_posts()[0].request.body) == {
        "title": "feat: algo",
        "description": "texto",
        "source": {"branch": {"name": "feature/REF-432-algo"}},
        "destination": {"branch": {"name": "pre"}},
        "close_source_branch": True,
    }
    # Ni miembros, ni usuario, ni default reviewers: solo buscar PR y crear.
    assert [c.request.method for c in responses.calls] == ["GET", "POST"]
    assert "warnings" not in result


@responses.activate
def test_tool_preview_without_reviewers_calls_nothing(server):
    result = _create(server)
    assert result["preview"] is True
    assert "reviewers" not in result
    assert len(responses.calls) == 0


@responses.activate
def test_tool_two_reviewers_preview_then_create(server):
    _add_lookups(defaults=(CARLOS,))
    preview = _create(server, reviewers=["ana", "acc-bea"])

    assert preview["preview"] is True
    assert preview["reviewers"] == [
        {"display_name": "Ana García", "account_id": "acc-ana"},
        {"display_name": "Bea López", "account_id": "acc-bea"},
    ]
    assert preview["default_reviewers"] == [{"display_name": "Carlos Ruiz", "account_id": "acc-carlos"}]
    assert _posts() == []

    responses.calls.reset()
    _add_no_open_pr()
    body = _pr_body()
    body["reviewers"] = [ANA, BEA, CARLOS]
    responses.add(responses.POST, PRS, json=body, status=201)

    result = _create(server, reviewers=["ana", "acc-bea"], confirm=True)

    sent = json.loads(_posts()[0].request.body)
    assert sent["reviewers"] == [
        {"account_id": "acc-ana"},
        {"account_id": "acc-bea"},
        {"account_id": "acc-carlos"},
    ]
    assert result["created"] is True
    assert [r["account_id"] for r in result["reviewers"]] == ["acc-ana", "acc-bea", "acc-carlos"]


@responses.activate
def test_tool_unknown_reviewer_errors_without_post(server):
    _add_no_open_pr()
    _add_lookups()

    preview = _create(server, reviewers=["ana", "fantasma"])
    result = _create(server, reviewers=["ana", "fantasma"], confirm=True)

    assert "fantasma" in preview["error"]
    assert "fantasma" in result["error"]
    assert _posts() == []


@responses.activate
def test_tool_author_in_list_is_removed_and_warned(server):
    _add_no_open_pr()
    _add_lookups()
    body = _pr_body()
    body["reviewers"] = [ANA]
    responses.add(responses.POST, PRS, json=body, status=201)

    preview = _create(server, reviewers=["ana", "luis"])
    result = _create(server, reviewers=["ana", "luis"], confirm=True)

    assert [r["account_id"] for r in preview["reviewers"]] == ["acc-ana"]
    assert "autor" in preview["warnings"][0]
    assert json.loads(_posts()[0].request.body)["reviewers"] == [{"account_id": "acc-ana"}]
    assert "autor" in result["warnings"][0]


@responses.activate
def test_tool_reviewer_outside_allowlist_errors_without_post(server, monkeypatch):
    _use_config(server, monkeypatch, make_config(bitbucket_allowed_reviewers=("acc-ana",)))
    _add_no_open_pr()
    _add_lookups()

    result = _create(server, reviewers=["ana", "bea"], confirm=True)

    assert "BITBUCKET_ALLOWED_REVIEWERS" in result["error"]
    assert "Bea López" in result["error"]
    assert _posts() == []


@responses.activate
def test_tool_allowlist_accepts_listed_reviewer(server, monkeypatch):
    _use_config(server, monkeypatch, make_config(bitbucket_allowed_reviewers=("acc-ana",)))
    _add_lookups()

    preview = _create(server, reviewers=["ana"])

    assert [r["account_id"] for r in preview["reviewers"]] == ["acc-ana"]


@responses.activate
def test_tool_existing_pr_is_returned_and_reviewers_not_applied(server):
    responses.add(responses.GET, PRS, json={"values": [_pr_body(pr_id=5)]})

    result = _create(server, reviewers=["ana"], confirm=True)

    assert result["already_exists"] is True
    assert result["id"] == 5
    assert "NO se han aplicado" in result["warnings"][0]
    assert _posts() == []


@responses.activate
def test_tool_existing_pr_without_reviewers_has_no_warning(server):
    responses.add(responses.GET, PRS, json={"values": [_pr_body(pr_id=5)]})

    result = _create(server, confirm=True)

    assert result["already_exists"] is True
    assert "warnings" not in result


@responses.activate
def test_get_pull_request_includes_reviewers(server):
    body = _pr_body()
    body["reviewers"] = [ANA]
    responses.add(responses.GET, f"{PRS}/42", json=body)

    detail = server.get_pull_request(42)

    assert detail["reviewers"] == [{"display_name": "Ana García", "account_id": "acc-ana"}]
