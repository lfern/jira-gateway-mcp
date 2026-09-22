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
