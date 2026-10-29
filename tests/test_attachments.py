import hashlib
import importlib
import os

import pytest
import responses

from gateway.attachments import AttachmentError, Inbox, inbox_from_config, validate_filename, validate_filenames
from gateway.jira_client import JiraClient, JiraError
from tests.conftest import make_config

BASE = "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3"
PNG = b"\x89PNG\r\n\x1a\nfalso"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add_issue(key="PROJ-7", project="PROJ"):
    responses.add(responses.GET, f"{BASE}/issue/{key}", json={"key": key, "fields": {"project": {"key": project}}})


def _add_meta(enabled=True, limit=10_000_000):
    responses.add(responses.GET, f"{BASE}/attachment/meta", json={"enabled": enabled, "uploadLimit": limit})


def _add_upload(*names):
    responses.add(
        responses.POST,
        f"{BASE}/issue/PROJ-7/attachments",
        json=[
            {"id": str(10000 + i), "filename": n, "size": 10, "mimeType": "image/png", "author": {"displayName": "Yo"}}
            for i, n in enumerate(names)
        ],
    )


@pytest.fixture
def inbox_dir(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture
def inbox(inbox_dir):
    return Inbox(str(inbox_dir), max_bytes=1024)


@pytest.fixture
def server(monkeypatch, inbox_dir):
    for name, value in {
        "JIRA_EMAIL": "jira@example.com",
        "JIRA_API_TOKEN": "t",
        "JIRA_CLOUD_ID": "cloud-id",
        "JIRA_SITE_URL": "https://example.atlassian.net",
        "JIRA_PROJECT_KEY": "PROJ",
        "JIRA_ATTACHMENTS_DIR": str(inbox_dir),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    import gateway.server

    module = importlib.reload(gateway.server)
    monkeypatch.setattr(module, "_jira", JiraClient(make_config()))
    return module


def _writes():
    return [c for c in responses.calls if c.request.method != "GET"]


# --- nombres ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["captura.png", "Captura de pantalla 2026-10-29 (2).PNG", "error_ñ.log", "a.pdf"])
def test_validate_filename_accepts_plain_names(name):
    assert validate_filename(name) == name


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".env",
        ".hidden.png",
        "../x.png",
        "dir/x.png",
        "/opt/jira-gateway-acme/.env",
        "x.png/",
        "..",
        "x\\y.png",
        "_x.png",
        "x.png\n",
    ],
)
def test_validate_filename_rejects_paths_and_hidden(bad):
    with pytest.raises(AttachmentError):
        validate_filename(bad)


@pytest.mark.parametrize("bad", ["x.env", "x.py", "x.sh", "x", "x.png.exe", "x.zip"])
def test_validate_filename_rejects_other_types(bad):
    with pytest.raises(AttachmentError, match="no permitido"):
        validate_filename(bad)


def test_validate_filenames_limits():
    with pytest.raises(AttachmentError):
        validate_filenames([])
    with pytest.raises(AttachmentError, match="repetidos"):
        validate_filenames(["a.png", "a.png"])
    with pytest.raises(AttachmentError, match="máximo"):
        validate_filenames([f"a{i}.png" for i in range(11)])


# --- lectura de la bandeja --------------------------------------------------------------


def test_read_returns_content_type_and_hash(inbox, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    f = inbox.read("c.png")
    assert (f.content, f.mime_type, f.sha256, f.size) == (PNG, "image/png", _sha(PNG), len(PNG))


def test_read_missing_file_mentions_inbox(inbox, inbox_dir):
    with pytest.raises(AttachmentError, match=str(inbox_dir)):
        inbox.read("no.png")


def test_read_rejects_symlink_out_of_inbox(inbox, inbox_dir, tmp_path):
    secret = tmp_path / "secreto.txt"
    secret.write_text("JIRA_API_TOKEN=x")
    os.symlink(secret, inbox_dir / "c.txt")
    with pytest.raises(AttachmentError, match="enlace"):
        inbox.read("c.txt")


def test_read_rejects_hard_link(inbox, inbox_dir, tmp_path):
    secret = tmp_path / "secreto.txt"
    secret.write_text("JIRA_API_TOKEN=x")
    os.link(secret, inbox_dir / "c.txt")
    with pytest.raises(AttachmentError, match="hard link"):
        inbox.read("c.txt")


def test_read_rejects_directory_and_fifo(inbox, inbox_dir):
    (inbox_dir / "d.png").mkdir()
    os.mkfifo(inbox_dir / "f.png")
    for name in ("d.png", "f.png"):
        with pytest.raises(AttachmentError):
            inbox.read(name)


def test_read_rejects_too_big_and_empty(inbox, inbox_dir):
    (inbox_dir / "big.png").write_bytes(b"x" * 1025)
    (inbox_dir / "empty.png").write_bytes(b"")
    with pytest.raises(AttachmentError, match="máximo"):
        inbox.read("big.png")
    with pytest.raises(AttachmentError, match="vacío"):
        inbox.read("empty.png")


def test_list_shows_only_uploadable_files(inbox, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    (inbox_dir / ".oculto.png").write_bytes(PNG)
    (inbox_dir / "script.sh").write_text("x")
    (inbox_dir / "d.png").mkdir()
    assert inbox.list() == [{"filename": "c.png", "size": len(PNG), "windows_path": None}]


def test_inbox_from_config(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert inbox_from_config(None, 10, None) is None
    inbox = inbox_from_config("/srv/inbox", 2, None)
    assert inbox.max_bytes == 2 * 1024 * 1024
    assert inbox.windows_path("c.png") == "\\\\wsl.localhost\\Ubuntu\\srv\\inbox\\c.png"
    assert inbox_from_config("/srv/inbox", 2, "Z:\\inbox\\").windows_path("c.png") == "Z:\\inbox\\c.png"


# --- cliente de Jira -------------------------------------------------------------------


@responses.activate
def test_client_uploads_multipart_without_json_content_type(cfg):
    _add_issue()
    _add_meta()
    _add_upload("a.png", "b.png")

    result = JiraClient(cfg).add_attachments("PROJ-7", [("a.png", PNG, "image/png"), ("b.png", PNG, "image/png")])

    assert [a["filename"] for a in result] == ["a.png", "b.png"]
    req = _writes()[0].request
    assert req.headers["X-Atlassian-Token"] == "no-check"
    assert req.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert req.body.count(b'name="file"') == 2


@responses.activate
def test_client_rejects_issue_moved_to_other_project(cfg):
    responses.add(responses.GET, f"{BASE}/issue/PROJ-7", json={"key": "OTRO-1", "fields": {"project": {"key": "OTRO"}}})
    with pytest.raises(JiraError):
        JiraClient(cfg).add_attachments("PROJ-7", [("a.png", PNG, "image/png")])
    assert _writes() == []


@responses.activate
def test_client_respects_jira_limits(cfg):
    _add_issue()
    _add_meta(limit=3)
    with pytest.raises(JiraError, match="límite"):
        JiraClient(cfg).add_attachments("PROJ-7", [("a.png", PNG, "image/png")])
    assert _writes() == []


@responses.activate
def test_client_fails_when_attachments_disabled(cfg):
    _add_issue()
    _add_meta(enabled=False)
    with pytest.raises(JiraError, match="desactivados"):
        JiraClient(cfg).add_attachments("PROJ-7", [("a.png", PNG, "image/png")])
    assert _writes() == []


# --- tools ------------------------------------------------------------------------------


@responses.activate
def test_add_attachments_preview_sends_nothing(server, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)

    result = server.add_attachments("PROJ-7", ["c.png"])

    assert result["preview"] is True
    assert result["sha256"] == [_sha(PNG)]
    assert result["files"][0]["path"] == str(inbox_dir / "c.png")
    assert result["files"][0]["mime_type"] == "image/png"
    assert len(responses.calls) == 0
    assert (inbox_dir / "c.png").exists()


@responses.activate
def test_add_attachments_confirm_uploads_and_empties_inbox(server, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    _add_issue()
    _add_meta()
    _add_upload("c.png")

    result = server.add_attachments("PROJ-7", ["c.png"], sha256=[_sha(PNG)], confirm=True)

    assert result["created"] is True
    assert result["attachments"][0]["id"] == "10000"
    assert "warning" not in result
    assert not (inbox_dir / "c.png").exists()


@responses.activate
def test_add_attachments_confirm_requires_hashes(server, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    assert "sha256" in server.add_attachments("PROJ-7", ["c.png"], confirm=True)["error"]
    assert len(responses.calls) == 0


@responses.activate
def test_add_attachments_confirm_rejects_file_changed_after_preview(server, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    old = server.add_attachments("PROJ-7", ["c.png"])["sha256"]
    (inbox_dir / "c.png").write_bytes(PNG + b"otra captura")

    result = server.add_attachments("PROJ-7", ["c.png"], sha256=old, confirm=True)

    assert "cambiado" in result["error"]
    assert len(responses.calls) == 0
    assert (inbox_dir / "c.png").exists()


def test_add_attachments_rejects_paths_even_with_confirm(server):
    result = server.add_attachments("PROJ-7", ["/opt/jira-gateway-acme/.env"], sha256=["x"], confirm=True)
    assert "no válido" in result["error"]


def test_add_attachments_rejects_other_project(server, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    assert "no pertenece" in server.add_attachments("OTRO-1", ["c.png"])["error"]


@responses.activate
def test_add_attachments_jira_error_keeps_file(server, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    _add_issue()
    _add_meta()
    responses.add(responses.POST, f"{BASE}/issue/PROJ-7/attachments", status=403, body="sin permiso")

    result = server.add_attachments("PROJ-7", ["c.png"], sha256=[_sha(PNG)], confirm=True)

    assert "403" in result["error"]
    assert (inbox_dir / "c.png").exists()


def test_list_pending_attachments(server, inbox_dir):
    (inbox_dir / "c.png").write_bytes(PNG)
    result = server.list_pending_attachments()
    assert result["dir"] == str(inbox_dir)
    assert [f["filename"] for f in result["files"]] == ["c.png"]


def test_attachment_tools_without_inbox(server, monkeypatch):
    monkeypatch.setattr(server, "_inbox", None)
    assert "JIRA_ATTACHMENTS_DIR" in server.list_pending_attachments()["error"]
    assert "JIRA_ATTACHMENTS_DIR" in server.add_attachments("PROJ-7", ["c.png"])["error"]
