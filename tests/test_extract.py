"""Extraction hardening: text decoding, HTML, zip bombs, YouTube ids, size caps."""

import codecs
import io
import zipfile

import pytest

from app import extract
from app.extract import (
    ExtractError,
    decode_text,
    extract_bytes,
    extract_docx,
    html_to_text,
    youtube_video_id,
)
from app.moodle import MoodleClient, MoodleError


@pytest.mark.parametrize("blob", [
    codecs.BOM_UTF8 + "Größe".encode(),
    "Größe".encode("utf-16"),  # BOM + LE
    codecs.BOM_UTF16_BE + "Größe".encode("utf-16-be"),
    "Größe".encode("utf-32"),
    "Größe".encode(),
])
def test_decode_text_honours_boms(blob):
    assert decode_text(blob) == "Größe"


def test_decode_text_falls_back_to_latin1_and_drops_nuls():
    assert decode_text("café".encode("latin-1")) == "café"
    assert decode_text(b"a\x00b") == "ab"


def test_text_files_decode_through_extract_bytes():
    assert extract_bytes("notes".encode("utf-16"), "text/plain", "n.txt") == "notes"


def test_html_to_text_drops_markup():
    html = ("<html><head><title>T</title><style>p{color:red}</style></head><body>"
            "<h1>Trees</h1><p>A &amp; B<br>root</p><script>alert(1)</script>"
            "<ul><li>leaf</li><li>node</li></ul>"
            "<table><tr><td>a</td><td>b</td></tr></table></body></html>")
    text = html_to_text(html)
    assert "<" not in text and "alert" not in text and "color" not in text
    assert text == "Trees\n\nA & B\nroot\n\n- leaf\n- node\n\na | b"


@pytest.mark.parametrize("mime, name", [("text/html", "x"), (None, "page.html"),
                                        (None, "page.HTM")])
def test_html_files_are_converted(mime, name):
    assert extract_bytes(b"<p>Hello <b>world</b></p>", mime, name) == "Hello world"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_zip_bomb_docx_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(extract, "MAX_UNZIPPED_BYTES", 1000)
    bomb = _zip({"word/document.xml": b"\0" * 5000})
    assert len(bomb) < 1000  # small on the wire, large once inflated
    with pytest.raises(ExtractError, match="unpacks to over"):
        extract_docx(bomb)


def test_zip_with_too_many_members_is_refused(monkeypatch):
    monkeypatch.setattr(extract, "MAX_ZIP_MEMBERS", 3)
    with pytest.raises(ExtractError, match="too many parts"):
        extract_bytes(_zip({f"f{i}": b"" for i in range(4)}), None, "x.pptx")


def test_non_zip_docx_is_an_extract_error():
    with pytest.raises(ExtractError, match="not a valid file"):
        extract_docx(b"not a zip")


@pytest.mark.parametrize("url, video_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ?start=3", "dQw4w9WgXcQ"),
    ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/live/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    (" https://youtu.be/dQw4w9WgXcQ ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/@lecturer/live", None),
    ("https://www.youtube.com/playlist?list=PL123", None),
    ("https://www.youtube.com/watch", None),
    ("https://notyoutu.be/dQw4w9WgXcQ", None),
    ("https://example.com/?next=youtu.be/dQw4w9WgXcQ", None),
    ("https://[bad", None),
    (None, None),
])
def test_youtube_video_id(url, video_id):
    assert youtube_video_id(url) == video_id


# -- Moodle download cap -------------------------------------------------------


class _Response:
    def __init__(self, body: bytes, length: str | None):
        self.body = io.BytesIO(body)
        self.reads = 0
        headers = {"Content-Type": "application/pdf"}
        if length is not None:
            headers["Content-Length"] = length
        from email.message import Message

        self.headers = Message()
        for k, v in headers.items():
            self.headers[k] = v

    def read(self, n=-1):
        self.reads += 1
        return self.body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _download(monkeypatch, response: _Response):
    import app.moodle as moodle

    monkeypatch.setattr(moodle.urllib.request, "urlopen", lambda req, timeout: response)
    client = MoodleClient("https://m.example", "tok")
    return client.download("https://m.example/webservice/pluginfile.php/1/a.pdf")


def test_download_refuses_large_content_length_without_reading(monkeypatch):
    import app.moodle as moodle

    monkeypatch.setattr(moodle, "MAX_DOWNLOAD_BYTES", 10)
    response = _Response(b"x" * 11, "11")
    with pytest.raises(ExtractError, match="over"):
        _download(monkeypatch, response)
    assert response.reads == 0


def test_download_stops_streaming_past_the_cap(monkeypatch):
    import app.moodle as moodle

    monkeypatch.setattr(moodle, "MAX_DOWNLOAD_BYTES", 10)
    with pytest.raises(ExtractError):  # permanent, not a retryable MoodleError
        _download(monkeypatch, _Response(b"x" * 11, None))


def test_download_within_the_cap(monkeypatch):
    blob, mime = _download(monkeypatch, _Response(b"%PDF-1.4 data", "13"))
    assert (blob, mime) == (b"%PDF-1.4 data", "application/pdf")


def test_download_network_errors_stay_retryable(monkeypatch):
    import app.moodle as moodle

    def boom(req, timeout):
        raise OSError("connection reset")

    monkeypatch.setattr(moodle.urllib.request, "urlopen", boom)
    with pytest.raises(MoodleError):
        MoodleClient("https://m.example", "tok").download(
            "https://m.example/pluginfile.php/1/a.pdf")
