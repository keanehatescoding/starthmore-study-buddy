"""Drive downloads for Classroom materials (issue #37), against a fake service."""

import pytest

from app import drive
from app.extract import ExtractError

FID = "1AbCdEfGhIjKlMnOp"


class _Req:
    def __init__(self, result):
        self.result = result

    def execute(self, num_retries=0):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Files:
    def __init__(self, meta, media=b"", exported=b"", fail=None):
        self.meta, self.media, self.exported, self.fail = meta, media, exported, fail
        self.calls = []

    def get(self, fileId, fields, supportsAllDrives):
        self.calls.append(("get", fileId))
        return _Req(self.fail or self.meta)

    def get_media(self, fileId, supportsAllDrives):
        self.calls.append(("media", fileId))
        return _Req(self.media)

    def export(self, fileId, mimeType):
        self.calls.append(("export", mimeType))
        return _Req(self.exported)


class _Service:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def _client(**kw):
    files = _Files(**kw)
    return drive.DriveClient(_Service(files)), files


def _http_error(status, content=b"{}"):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    return HttpError(Response({"status": status}), content)


@pytest.mark.parametrize("url", [
    f"https://drive.google.com/file/d/{FID}/view?usp=drive_web",
    f"https://docs.google.com/document/d/{FID}/edit",
    f"https://drive.google.com/open?id={FID}",
])
def test_file_id_from_drive_urls(url):
    assert drive.file_id(url) == FID


def test_file_id_rejects_other_urls():
    assert drive.file_id("https://example.com/notes.pdf") is None
    assert drive.file_id(None) is None


def test_binary_file_downloads_media():
    client, files = _client(meta={"mimeType": "application/pdf", "size": "10"},
                            media=b"%PDF")
    assert client.download(f"https://drive.google.com/file/d/{FID}/view") == (
        b"%PDF", "application/pdf")
    assert files.calls == [("get", FID), ("media", FID)]


@pytest.mark.parametrize("google_type, export_mime", sorted(drive.EXPORTS.items()))
def test_google_files_are_exported(google_type, export_mime):
    client, files = _client(meta={"mimeType": google_type}, exported=b"slide text")
    blob, mime = client.download(f"https://docs.google.com/x/d/{FID}/edit")
    assert (blob, mime) == (b"slide text", export_mime)
    assert ("media", FID) not in files.calls


def test_unsupported_google_type_and_oversize_fail_permanently(monkeypatch):
    client, _ = _client(meta={"mimeType": "application/vnd.google-apps.form"})
    with pytest.raises(ExtractError, match="unsupported"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")
    client, files = _client(meta={"mimeType": "application/pdf",
                                  "size": str(drive.MAX_BYTES + 1)})
    with pytest.raises(ExtractError, match="MB"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")
    assert ("media", FID) not in files.calls  # checked before downloading


def test_missing_file_fails_permanently():
    client, _ = _client(meta={}, fail=_http_error(404))
    with pytest.raises(ExtractError, match="not found"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")


def test_token_without_drive_grant_stays_retryable():
    from google.auth.exceptions import RefreshError

    client, _ = _client(meta={}, fail=RefreshError("invalid_scope"))
    with pytest.raises(drive.DriveError, match="sign in again"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")
    body = b'{"error": {"errors": [{"reason": "insufficientPermissions"}]}}'
    client, _ = _client(meta={}, fail=_http_error(403, body))
    with pytest.raises(drive.DriveError, match="sign in again"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")


def test_server_error_stays_retryable():
    client, _ = _client(meta={}, fail=_http_error(503))
    with pytest.raises(drive.DriveError):
        client.download(f"https://drive.google.com/file/d/{FID}/view")


def test_non_drive_url_fails_permanently():
    client, files = _client(meta={})
    with pytest.raises(ExtractError, match="not a Drive"):
        client.download("https://example.com/notes.pdf")
    assert files.calls == []
