import json

import pytest
import responses

from gateway.bitbucket_client import (
    BitbucketClient,
    BitbucketError,
    validate_build_number,
    validate_pipeline_pattern,
    validate_ref_type,
)
from tests.conftest import make_config


def _pipeline_body(build_number=7, state_name="PENDING", result_name=None):
    state = {"name": state_name}
    if result_name:
        state["result"] = {"name": result_name}
    return {
        "uuid": "{pipeline-uuid}",
        "build_number": build_number,
        "state": state,
    }


# --- Validación de pattern/ref_type/build_number -----------------------------


def test_validate_pipeline_pattern_accepts_allowed(cfg):
    validate_pipeline_pattern("sello-version-pre", cfg)  # no debe lanzar


def test_validate_pipeline_pattern_rejects_unlisted(cfg):
    with pytest.raises(BitbucketError, match="no permitido"):
        validate_pipeline_pattern("deploy-a-mano", cfg)


def test_validate_pipeline_pattern_rejects_when_allowlist_empty():
    cfg = make_config(bitbucket_allowed_pipelines=())
    with pytest.raises(BitbucketError, match="BITBUCKET_ALLOWED_PIPELINES"):
        validate_pipeline_pattern("sello-version-pre", cfg)


@pytest.mark.parametrize("bad", ["", "con espacios", "../escape", "a" * 101])
def test_validate_pipeline_pattern_rejects_bad_format(cfg, bad):
    with pytest.raises(BitbucketError):
        validate_pipeline_pattern(bad, cfg)


@pytest.mark.parametrize("good", ["branch", "tag"])
def test_validate_ref_type_accepts_branch_and_tag(good):
    validate_ref_type(good)  # no debe lanzar


@pytest.mark.parametrize("bad", ["commit", "", "Branch"])
def test_validate_ref_type_rejects_others(bad):
    with pytest.raises(BitbucketError):
        validate_ref_type(bad)


def test_validate_build_number_accepts_positive_int():
    assert validate_build_number(7) == 7


@pytest.mark.parametrize("bad", [0, -1, "7", True])
def test_validate_build_number_rejects_bad_values(bad):
    with pytest.raises(BitbucketError):
        validate_build_number(bad)


# --- run_pipeline: payload y respuesta ----------------------------------------


@responses.activate
def test_run_pipeline_builds_expected_payload(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.POST,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pipelines/",
        json=_pipeline_body(),
        status=201,
    )

    result = client.run_pipeline(
        repo_slug="refunder-react",
        pattern="sello-version-pre",
        ref_name="pre",
        ref_type="branch",
    )

    sent = json.loads(responses.calls[0].request.body)
    assert sent == {
        "target": {
            "type": "pipeline_ref_target",
            "ref_type": "branch",
            "ref_name": "pre",
            "selector": {"type": "custom", "pattern": "sello-version-pre"},
        }
    }

    assert result == {
        "build_number": 7,
        "uuid": "{pipeline-uuid}",
        "state": "PENDING",
        "url": "https://bitbucket.org/dividend-refund/refunder-react/pipelines/results/7",
    }


def test_run_pipeline_rejects_pattern_outside_allowlist_without_http_call(cfg):
    client = BitbucketClient(cfg)
    with pytest.raises(BitbucketError, match="no permitido"):
        client.run_pipeline(
            repo_slug="refunder-react",
            pattern="deploy-produccion-a-mano",
            ref_name="pre",
            ref_type="branch",
        )


def test_run_pipeline_rejects_bad_ref_type_without_http_call(cfg):
    client = BitbucketClient(cfg)
    with pytest.raises(BitbucketError, match="ref_type"):
        client.run_pipeline(
            repo_slug="refunder-react",
            pattern="sello-version-pre",
            ref_name="pre",
            ref_type="commit",
        )


# --- Traducción de errores -----------------------------------------------------


@responses.activate
def test_run_pipeline_400_surfaces_bitbucket_message(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.POST,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pipelines/",
        json={"type": "error", "error": {"message": "No pipeline matching pattern 'sello-version-pre'."}},
        status=400,
    )

    with pytest.raises(BitbucketError, match="No pipeline matching pattern"):
        client.run_pipeline(
            repo_slug="refunder-react",
            pattern="sello-version-pre",
            ref_name="pre",
            ref_type="branch",
        )


@responses.activate
def test_run_pipeline_403_gives_explicit_scope_hint_without_leaking_token(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.POST,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pipelines/",
        json={"type": "error", "error": {"message": "You do not have access to this resource."}},
        status=403,
    )

    with pytest.raises(BitbucketError) as exc_info:
        client.run_pipeline(
            repo_slug="refunder-react",
            pattern="sello-version-pre",
            ref_name="pre",
            ref_type="branch",
        )

    message = str(exc_info.value)
    assert "403" in message
    assert "scope" in message.lower() or "pipelines" in message.lower()
    assert cfg.bitbucket_api_token not in message
    assert "Authorization" not in message


@responses.activate
def test_get_pipeline_401_gives_explicit_scope_hint(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pipelines/7",
        json={"type": "error", "error": {"message": "Unauthorized"}},
        status=401,
    )

    with pytest.raises(BitbucketError, match="401"):
        client.get_pipeline("refunder-react", 7)


# --- get_pipeline: parseo de estado/resultado ----------------------------------


@responses.activate
def test_get_pipeline_in_progress_has_no_result(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pipelines/7",
        json=_pipeline_body(build_number=7, state_name="IN_PROGRESS"),
        status=200,
    )

    detail = client.get_pipeline("refunder-react", 7)

    assert detail["state"] == "IN_PROGRESS"
    assert detail["result"] is None


@responses.activate
def test_get_pipeline_completed_has_result(cfg):
    client = BitbucketClient(cfg)
    responses.add(
        responses.GET,
        "https://api.bitbucket.org/2.0/repositories/dividend-refund/refunder-react/pipelines/7",
        json=_pipeline_body(build_number=7, state_name="COMPLETED", result_name="SUCCESSFUL"),
        status=200,
    )

    detail = client.get_pipeline("refunder-react", 7)

    assert detail["state"] == "COMPLETED"
    assert detail["result"] == "SUCCESSFUL"
    assert detail["url"] == "https://bitbucket.org/dividend-refund/refunder-react/pipelines/results/7"
