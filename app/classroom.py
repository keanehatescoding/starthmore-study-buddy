"""Google Classroom client. OAuth2 Web-application flow, refresh-token reuse.

Required scopes (coursework alone misses materials/topics):
  classroom.courses.readonly, classroom.coursework.me.readonly,
  classroom.courseworkmaterials.readonly, classroom.topics.readonly
  (+ optionally classroom.announcements.readonly)

google-* imports are lazy so the module (and tests with fakes) load
without the google libs installed.

Drive-file materials sync as metadata (hash of the Drive file id) with a
Drive URL; the pipeline downloads them via app.drive under the owner's
drive.readonly grant. Edits inside the same Drive file aren't seen as a
change; swapping the attachment for another file (new id) or renaming it is.

Materials with no topic, or whose topic was deleted, sync under a synthetic
"Other materials" topic (UNTAGGED_TOPIC_ID) instead of being dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
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
        scopes=SCOPES,
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
        # course id -> (known topic ids, materials); refreshed by fetch_topics
        # so each course sync lists materials once, not once per topic.
        self._materials: dict[str, tuple[set[str], list[dict]]] = {}

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
        materials = self.client.list_materials(course_source_id)
        self._materials[course_source_id] = (known, materials)
        if any(_topic_of(m, known) == UNTAGGED_TOPIC_ID for m in materials):
            topics.append(TopicData(UNTAGGED_TOPIC_ID, UNTAGGED_TITLE, len(topics)))
        return topics

    def fetch_resources(
        self, course_source_id: str, topic_source_id: str
    ) -> list[ResourceData]:
        if course_source_id not in self._materials:
            self.fetch_topics(course_source_id)
        known, materials = self._materials[course_source_id]
        out: list[ResourceData] = []
        for m in materials:
            if _topic_of(m, known) != topic_source_id:
                continue
            for i, mat in enumerate(m.get("materials", [])):
                sid = f"{m['id']}:{i}"
                title = m.get("title") or "Material"
                if "youtubeVideo" in mat:
                    vid = mat["youtubeVideo"]
                    url = vid.get("alternateLink") or (
                        f"https://www.youtube.com/watch?v={vid.get('id')}"
                    )
                    out.append(
                        ResourceData(
                            topic_source_id, sid, "video", title,
                            raw_url=url, content_bytes=f"yt:{vid.get('id')}".encode(),
                        )
                    )
                elif "link" in mat:
                    url = mat["link"]["url"]
                    out.append(
                        ResourceData(
                            topic_source_id, sid, link_type(url), title,
                            raw_url=url, content_bytes=url.encode(),
                        )
                    )
                elif "driveFile" in mat:
                    # SharedDriveFile{driveFile: DriveFile{id, title, alternateLink}}
                    df = mat["driveFile"].get("driveFile") or {}
                    out.append(
                        ResourceData(
                            topic_source_id, sid, "file", df.get("title") or title,
                            raw_url=_drive_url(df),
                            content_bytes=_drive_marker(df),
                        )
                    )
                elif "form" in mat:
                    url = mat["form"].get("formUrl", "")
                    out.append(
                        ResourceData(
                            topic_source_id, sid, "link", mat["form"].get("title") or title,
                            raw_url=url, content_bytes=url.encode(),
                        )
                    )
        return out

    def fetch_assignments(self, course_source_id: str) -> list[AssignmentData]:
        out: list[AssignmentData] = []
        for w in self.client.list_coursework(course_source_id):
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


UNTAGGED_TOPIC_ID = "untagged"  # Classroom topic ids are numeric; can't collide
UNTAGGED_TITLE = "Other materials"


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
