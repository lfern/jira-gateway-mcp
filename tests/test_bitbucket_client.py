import json

import pytest
import responses

from gateway.bitbucket_client import (
    BitbucketClient,
    BitbucketError,
    validate_branch_name,
    validate_pr_id,
    validate_repo_slug,
)
from tests.conftest import make_config


def _pr_body(
    pr_id=42,
    title="feat: algo",
    source="feature/REF-432-algo",
    destination="pre",
    state="OPEN",
    author="Luis",
):
    return {
        "id": pr_id,
        "title": title,
        "description": "texto",
        "state": state,
        "close_source_branch": True,
        "created_on": "2026-09-22T10:00:00Z",
        "updated_on": "2026-09-22T10:00:00Z",
        "source": {"branch": {"name": source}},
        "destination": {"branch": {"name": destination}},
        "author": {"display_name": author},
        "links": {
            "self": {"href": f"https://api.bitbucket.org/2.0/.../pullrequests/{pr_id}"},
            "html": {"href": f"https://bitbucket.org/dividend-refund/refunder-react/pull-requests/{pr_id}"},
        },
    }


# --- Validación de ramas ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "feature/REF-432-algo",
        "pre",
        "staging",
        "master",
        "fix.something",
        "a",
    ],
)
def test_validate_branch_name_accepts_valid_names(name):
    validate_branch_name(name)  # no debe lanzar


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../etc/passwd",
        "feature/../secret",
        "con espacios",
        "trailing/",
        "double//slash",
        "-empieza-con-guion",
        "rama.lock",
        "a" * 201,
    ],
)
def test_validate_branch_name_rejects_invalid_names(name):
    with pytest.raises(BitbucketError):
        validate_branch_name(name)


# --- Allowlist de repos ------------------------------------------------------


def test_validate_repo_slug_accepts_allowed_repo(cfg):
    validate_repo_slug("refunder-react", cfg)  # no debe lanzar


def test_validate_repo_slug_rejects_unlisted_repo(cfg):
    with pytest.raises(BitbucketError, match="no permitido"):
        validate_repo_slug("otro-repo-cualquiera", cfg)


def test_validate_repo_slug_rejects_when_allowlist_empty():
    cfg = make_config(bitbucket_allowed_repos=())
    with pytest.raises(BitbucketError, match="BITBUCKET_ALLOWED_REPOS"):
        validate_repo_slug("refunder-react", cfg)


# --- pr_id -------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, "42", 3.5, True])
def test_validate_pr_id_rejects_bad_values(bad):
    with pytest.raises(BitbucketError):
        validate_pr_id(bad)


def test_validate_pr_id_accepts_positive_int():
    assert validate_pr_id(7) == 7


# --- create_pull_request: payload y respuesta --------------------------------


@responses.activate
def test_create_pull_request_builds_expected_payload(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.POST,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests",
        json=_pr_body(),
        status=201,
    )

    result = client.create_pull_request(
        repo_slug="refunder-react",
        source_branch="feature/REF-432-algo",
        target_branch="pre",
        title="feat: algo",
        description="texto",
        close_source_branch=True,
    )

    sent = json.loads(responses.calls[0].request.body)
    assert sent == {
        "title": "feat: algo",
        "description": "texto",
        "source": {"branch": {"name": "feature/REF-432-algo"}},
        "destination": {"branch": {"name": "pre"}},
        "close_source_branch": True,
    }
    assert "reviewers" not in sent  # sin reviewers -> Bitbucket aplica los default del repo

    assert result == {
        "id": 42,
        "url": "https://bitbucket.org/dividend-refund/refunder-react/pull-requests/42",
        "state": "OPEN",
        "source": "feature/REF-432-algo",
        "destination": "pre",
    }


@responses.activate
def test_create_pull_request_with_reviewers_maps_to_uuid_objects(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.POST,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests",
        json=_pr_body(),
        status=201,
    )

    client.create_pull_request(
        repo_slug="refunder-react",
        source_branch="feature/REF-432-algo",
        target_branch="pre",
        title="t",
        description="d",
        close_source_branch=True,
        reviewers=["{uuid-1}", "{uuid-2}"],
    )

    sent = json.loads(responses.calls[0].request.body)
    assert sent["reviewers"] == [{"uuid": "{uuid-1}"}, {"uuid": "{uuid-2}"}]


def test_create_pull_request_rejects_repo_outside_allowlist_without_http_call(cfg):
    client = BitbucketClient(cfg)
    with pytest.raises(BitbucketError, match="no permitido"):
        client.create_pull_request(
            repo_slug="repo-no-permitido",
            source_branch="feature/x",
            target_branch="pre",
            title="t",
            description="d",
            close_source_branch=True,
        )


def test_create_pull_request_rejects_bad_branch_name_without_http_call(cfg):
    client = BitbucketClient(cfg)
    with pytest.raises(BitbucketError):
        client.create_pull_request(
            repo_slug="refunder-react",
            source_branch="../escape",
            target_branch="pre",
            title="t",
            description="d",
            close_source_branch=True,
        )


# --- Traducción de errores HTTP ----------------------------------------------


@responses.activate
def test_http_error_is_translated_without_leaking_token(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.POST,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests",
        json={"type": "error", "error": {"message": "Rama origen no existe"}},
        status=404,
    )

    with pytest.raises(BitbucketError) as exc_info:
        client.create_pull_request(
            repo_slug="refunder-react",
            source_branch="feature/no-existe",
            target_branch="pre",
            title="t",
            description="d",
            close_source_branch=True,
        )

    message = str(exc_info.value)
    assert "404" in message
    assert "Rama origen no existe" in message
    assert cfg.bitbucket_api_token not in message
    assert "Authorization" not in message


@responses.activate
def test_http_error_without_json_body_falls_back_to_text(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests/9",
        body="Internal Server Error",
        status=500,
        content_type="text/plain",
    )

    with pytest.raises(BitbucketError, match="500"):
        client.get_pull_request("refunder-react", 9)


# --- Listado y detalle ---------------------------------------------------------


@responses.activate
def test_list_open_pull_requests_returns_compact_list(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests",
        json={"values": [_pr_body(pr_id=1, source="feature/a"), _pr_body(pr_id=2, source="feature/b")]},
        status=200,
    )

    prs = client.list_open_pull_requests("refunder-react")

    assert [p["id"] for p in prs] == [1, 2]
    assert prs[0]["source"] == "feature/a"
    assert responses.calls[0].request.params["state"] == "OPEN"


@responses.activate
def test_get_pull_request_returns_detail(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests/42",
        json=_pr_body(),
        status=200,
    )

    detail = client.get_pull_request("refunder-react", 42)

    assert detail["id"] == 42
    assert detail["description"] == "texto"
    assert detail["close_source_branch"] is True


# --- find_open_pr_for_branch: evita duplicados --------------------------------


@responses.activate
def test_find_open_pr_for_branch_returns_match(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests",
        json={"values": [_pr_body(pr_id=5, source="feature/REF-432-algo")]},
        status=200,
    )

    found = client.find_open_pr_for_branch("refunder-react", "feature/REF-432-algo")

    assert found is not None
    assert found["id"] == 5


@responses.activate
def test_find_open_pr_for_branch_returns_none_when_no_match(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pullrequests",
        json={"values": [_pr_body(pr_id=5, source="feature/otra-rama")]},
        status=200,
    )

    found = client.find_open_pr_for_branch("refunder-react", "feature/REF-432-algo")

    assert found is None
