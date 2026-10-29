from tests.conftest import make_config


def test_bitbucket_configured_true_when_all_creds_present():
    cfg = make_config()
    assert cfg.bitbucket_configured is True


def test_bitbucket_configured_false_when_missing_any_field():
    cfg = make_config(bitbucket_api_token=None)
    assert cfg.bitbucket_configured is False


def test_bitbucket_auth_header_is_basic_base64():
    cfg = make_config(bitbucket_email="a@b.com", bitbucket_api_token="tok")
    import base64

    expected = "Basic " + base64.b64encode(b"a@b.com:tok").decode()
    assert cfg.bitbucket_auth_header == expected


_REQUIRED_ENV = {
    "JIRA_EMAIL": "a@b.com",
    "JIRA_API_TOKEN": "t",
    "JIRA_CLOUD_ID": "c",
    "JIRA_SITE_URL": "https://x.atlassian.net",
    "JIRA_PROJECT_KEY": "PROJ",
}


def test_attachments_config_from_env(monkeypatch):
    from gateway.config import Config

    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("JIRA_ATTACHMENTS_DIR", "/srv/inbox")
    monkeypatch.setenv("JIRA_ATTACHMENTS_MAX_MB", "5")
    cfg = Config.from_env()
    assert (cfg.jira_attachments_dir, cfg.jira_attachments_max_mb) == ("/srv/inbox", 5)


def test_attachments_max_mb_must_be_positive_integer(monkeypatch):
    import pytest

    from gateway.config import Config, ConfigError

    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    for bad in ("diez", "0", "-1"):
        monkeypatch.setenv("JIRA_ATTACHMENTS_MAX_MB", bad)
        with pytest.raises(ConfigError):
            Config.from_env()
