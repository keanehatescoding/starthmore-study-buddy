"""Google Classroom client. OAuth2 Web-application flow, refresh-token reuse.

Required scopes (coursework alone misses materials/topics):
  classroom.courses.readonly, classroom.coursework.me.readonly,
  classroom.courseworkmaterials.readonly, classroom.topics.readonly,
  classroom.announcements.readonly (tokens granted before it was added skip
  announcements until the owner signs in again)

google-* imports are lazy so the module (and tests with fakes) load
without the google libs installed.

Drive-file materials sync as metadata (hash of the Drive file id) with a
Drive URL; the pipeline downloads them via app.drive under the owner's
drive.readonly grant. Edits inside the same Drive file aren't seen as a
change; swapping the attachment for another file (new id) or renaming it is.

Attachments come from courseWorkMaterials, courseWork (slides posted on an
assignment) and announcements. Materials and coursework with no topic, or
whose topic was deleted, sync under a synthetic "Other materials" topic
(UNTAGGED_TOPIC_ID) instead of being dropped; announcements have no topic and
sync under a synthetic "Announcements" topic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
]


def build_service(client_id: str, client_secret: str, refresh_token: str):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        # No scopes: the refresh then mints a token for whatever was granted.
        # Asking for SCOPES would fail with invalid_scope on every token
        # granted before the announcements scope was added.
        scopes=None,
    )
    return build("classroom", "v1", credentials=creds)


def get_refresh_token(client_id: str, client_secret: str) -> str:
    """First-run browser consent flow. Prints/saves nothing — caller stores it."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": ["http://localhost"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0)
    return creds.refresh_token


class ClassroomClient:
    def __init__(self, service):
        self.service = service

    def _pages(self, collection, request):
        # list_next carries the token itself; the first call sends none.
        while request is not None:
            resp = request.execute(num_retries=3)  # backs off on 429/5xx
            yield resp
            request = collection.list_next(request, resp)

    def _list_all(self, collection, key: str, **params) -> list[dict]:
        out = []
        for page in self._pages(collection, collection.list(pageSize=100, **params)):
            out.extend(page.get(key, []))
        return out

    def list_courses(self) -> list[dict]:
        return self._list_all(self.service.courses(), "courses", courseStates=["ACTIVE"])

    def list_topics(self, course_id: str) -> list[dict]:
        # ListTopicResponse's array really is singular "topic" (as with
        # "courseWorkMaterial" below), unlike "courses"/"courseWork".
        return self._list_all(self.service.courses().topics(), "topic", courseId=course_id)

    def list_materials(self, course_id: str) -> list[dict]:
        return self._list_all(
            self.service.courses().courseWorkMaterials(), "courseWorkMaterial",
            courseId=course_id,
        )

    def list_coursework(self, course_id: str) -> list[dict]:
        return self._list_all(
            self.service.courses().courseWork(), "courseWork", courseId=course_id
        )

    def list_announcements(self, course_id: str) -> list[dict]:
        """[] when the token lacks the announcements scope (granted before it
        was requested); other errors propagate like any other list call."""
        try:
            return self._list_all(
                self.service.courses().announcements(), "announcements",
                courseId=course_id,
            )
        except Exception as e:
            if _status(e) == 403 and b"insufficient" in (_content(e) or b"").lower():
                log.info("course %s: token lacks the announcements scope", course_id)
                return []
            raise


def _status(e: Exception) -> int | None:
    resp = getattr(e, "resp", None)
    return getattr(resp, "status", None)


def _content(e: Exception) -> bytes | None:
    return getattr(e, "content", None)


# -- adapter -------------------------------------------------------------------

from app.sync import (  # noqa: E402
    AssignmentData,
    CourseData,
    ResourceData,
    TopicData,
    link_type,
)


class ClassroomAdapter:
    source = "classroom"

    def __init__(self, client: ClassroomClient):
        self.client = client
        # course id -> its resources across topics; built by fetch_topics so
        # each course sync lists every collection once, not once per topic.
        self._resources: dict[str, list[ResourceData]] = {}
        # course id -> courseWork, listed by fetch_topics, reused by
        # fetch_assignments
        self._coursework: dict[str, list[dict]] = {}

    def fetch_courses(self) -> list[CourseData]:
        return [
            CourseData(
                source_id=c["id"],
                name=c.get("name", ""),
                code=c.get("section"),
            )
            for c in self.client.list_courses()
        ]

    def fetch_topics(self, course_source_id: str) -> list[TopicData]:
        topics = [
            TopicData(source_id=t["topicId"], title=t.get("name", ""), order=i)
            for i, t in enumerate(self.client.list_topics(course_source_id))
        ]
        known = {t.source_id for t in topics}
        work = self.client.list_coursework(course_source_id)
        self._coursework[course_source_id] = work
        # Material ids keep their bare form so rows synced before coursework
        # and announcements were read keep matching.
        posts = [
            (_topic_of(m, known), m["id"], m.get("title") or "Material",
             m.get("materials", []))
            for m in self.client.list_materials(course_source_id)
        ]
        posts += [
            (_topic_of(w, known), f"work:{w['id']}", w.get("title") or "Coursework",
             w["materials"])
            for w in work if w.get("materials")
        ]
        posts += [
            (ANNOUNCEMENTS_TOPIC_ID, f"ann:{a['id']}", _announcement_title(a),
             a["materials"])
            for a in self.client.list_announcements(course_source_id)
            if a.get("materials")
        ]
        # Indexes count every attachment, so skipping one never renumbers
        # the rest.
        resources = [
            r
            for topic, prefix, title, attachments in posts
            for i, mat in enumerate(attachments)
            if (r := _resource(topic, f"{prefix}:{i}", title, mat)) is not None
        ]
        self._resources[course_source_id] = resources
        # a synthetic topic only when something syncs into it
        used = {r.topic_source_id for r in resources}
        for sid, title in ((UNTAGGED_TOPIC_ID, UNTAGGED_TITLE),
                           (ANNOUNCEMENTS_TOPIC_ID, ANNOUNCEMENTS_TITLE)):
            if sid in used:
                topics.append(TopicData(sid, title, len(topics)))
        return topics

    def fetch_resources(
        self, course_source_id: str, topic_source_id: str
    ) -> list[ResourceData]:
        if course_source_id not in self._resources:
            self.fetch_topics(course_source_id)
        return [r for r in self._resources[course_source_id]
                if r.topic_source_id == topic_source_id]

    def fetch_assignments(self, course_source_id: str) -> list[AssignmentData]:
        work = self._coursework.get(course_source_id)
        if work is None:
            work = self.client.list_coursework(course_source_id)
        out: list[AssignmentData] = []
        for w in work:
            due = None
            if w.get("dueDate"):
                d = w["dueDate"]
                # proto3 JSON omits zero fields ({"hours": 14} is 14:00, {} is
                # midnight); only a missing dueTime means end of day
                t = w.get("dueTime", {"hours": 23, "minutes": 59, "seconds": 59})
                due = datetime(
                    d["year"], d.get("month", 1), d.get("day", 1),
                    t.get("hours", 0), t.get("minutes", 0), t.get("seconds", 0),
                    tzinfo=timezone.utc,
                )
            out.append(
                AssignmentData(
                    source_id=w["id"],
                    title=w.get("title", "Coursework"),
                    topic_source_id=w.get("topicId"),
                    due_date=due,
                    description=w.get("description"),
                )
            )
        return out


def _resource(topic: str, sid: str, title: str, mat: dict) -> ResourceData | None:
    """One Classroom Material (attachment) as a resource; None for kinds
    there is nothing to fetch from."""
    if "youtubeVideo" in mat:
        vid = mat["youtubeVideo"]
        url = vid.get("alternateLink") or (
            f"https://www.youtube.com/watch?v={vid.get('id')}"
        )
        return ResourceData(
            topic, sid, "video", title,
            raw_url=url, content_bytes=f"yt:{vid.get('id')}".encode(),
        )
    if "link" in mat:
        url = mat["link"]["url"]
        return ResourceData(
            topic, sid, link_type(url), title, raw_url=url, content_bytes=url.encode(),
        )
    if "driveFile" in mat:
        # SharedDriveFile{driveFile: DriveFile{id, title, alternateLink}}
        df = mat["driveFile"].get("driveFile") or {}
        return ResourceData(
            topic, sid, "file", df.get("title") or title,
            raw_url=_drive_url(df), content_bytes=_drive_marker(df),
        )
    if "form" in mat:
        url = mat["form"].get("formUrl", "")
        return ResourceData(
            topic, sid, "link", mat["form"].get("title") or title,
            raw_url=url, content_bytes=url.encode(),
        )
    for kind in ("gem", "notebook"):  # Gemini Gems, NotebookLM notebooks
        url = (mat.get(kind) or {}).get("url")
        if url:
            return ResourceData(
                topic, sid, "link", mat[kind].get("title") or title,
                raw_url=url, content_bytes=url.encode(),
            )
    return None


def _announcement_title(announcement: dict) -> str:
    """Announcements have no title: use the first line of the text."""
    first = (announcement.get("text") or "").strip().split("\n", 1)[0].strip()
    if not first:
        return "Announcement"
    return first if len(first) <= 80 else first[:79].rstrip() + "…"


UNTAGGED_TOPIC_ID = "untagged"  # Classroom topic ids are numeric; can't collide
UNTAGGED_TITLE = "Other materials"
ANNOUNCEMENTS_TOPIC_ID = "announcements"
ANNOUNCEMENTS_TITLE = "Announcements"


def _topic_of(material: dict, known: set[str]) -> str:
    topic_id = material.get("topicId")
    return topic_id if topic_id in known else UNTAGGED_TOPIC_ID


def _drive_url(drive_file: dict) -> str | None:
    """alternateLink, else a URL built from the id so app.drive can fetch it."""
    if drive_file.get("alternateLink"):
        return drive_file["alternateLink"]
    fid = drive_file.get("id")
    return f"https://drive.google.com/file/d/{fid}/view" if fid else None


def _drive_marker(drive_file: dict) -> bytes | None:
    """Stable change marker: the Drive file id (link as fallback), never None
    while the attachment is identifiable."""
    ref = drive_file.get("id") or drive_file.get("alternateLink")
    return f"drive:{ref}".encode() if ref else None
