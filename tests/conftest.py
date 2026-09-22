import pytest

from gateway.config import Config


def make_config(**overrides) -> Config:
    defaults = dict(
        jira_email="jira@example.com",
        jira_api_token="jira-token",
        jira_cloud_id="cloud-id",
        jira_site_url="https://example.atlassian.net",
        project_key="PROJ",
        in_progress_status="In Progress",
        selected_status="Selected for Development",
        default_issue_type="Task",
        subtask_issue_type="Subtask",
        bitbucket_email="bb@example.com",
        bitbucket_api_token="bb-token-secreto",
        bitbucket_workspace="dividend-refund",
        bitbucket_allowed_repos=("refunder-react", "dividend-refund"),
        bitbucket_default_repo="refunder-react",
        bitbucket_default_target_branch="pre",
        bitbucket_allowed_pipelines=("sello-version-pre", "sello-version-staging", "sello-version-prod"),
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def cfg() -> Config:
    return make_config()
