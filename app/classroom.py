"""Google Classroom client. OAuth2 Web-application flow, refresh-token reuse.

Required scopes (coursework alone misses materials/topics):
  classroom.courses.readonly, classroom.coursework.me.readonly,
  classroom.courseworkmaterials.readonly, classroom.topics.readonly
  (+ optionally classroom.announcements.readonly)

google-* imports are lazy so the module (and tests with fakes) load
without the google libs installed.

Known v1 limitation: Drive-file materials need a Drive scope to download,
which the plan's minimal scope set deliberately excludes. Those resources
sync as metadata (hash of the Drive link) with status pending; extraction
will mark them failed/skipped with a clear error until the scope is added.
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

    def _pages(self, request):
        while request is not None:
            resp = request.execute()
            yield resp
            request = self.service.courses().list_next(request, resp)

    def list_courses(self) -> list[dict]:
        out = []
        req = self.service.courses().list(courseStates=["ACTIVE"], pageSize=100)
        for page in self._pages(req):
            out.extend(page.get("courses", []))
        return out

    def list_topics(self, course_id: str) -> list[dict]:
        out, token = [], None
        while True:
            resp = (
                self.service.courses()
                .topics()
                .list(courseId=course_id, pageSize=100, pageToken=token)
                .execute()
            )
            out.extend(resp.get("topic", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def list_materials(self, course_id: str) -> list[dict]:
        out, token = [], None
        while True:
            resp = (
                self.service.courses()
                .courseWorkMaterials()
                .list(courseId=course_id, pageSize=100, pageToken=token)
                .execute()
            )
            out.extend(resp.get("courseWorkMaterial", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def list_coursework(self, course_id: str) -> list[dict]:
        out, token = [], None
        while True:
            resp = (
                self.service.courses()
                .courseWork()
                .list(courseId=course_id, pageSize=100, pageToken=token)
                .execute()
            )
            out.extend(resp.get("courseWork", []))
            token = resp.get("nextPageToken")
            if not token:
                return out


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
        return [
            TopicData(source_id=t["topicId"], title=t.get("name", ""), order=i)
            for i, t in enumerate(self.client.list_topics(course_source_id))
        ]

    def fetch_resources(
        self, course_source_id: str, topic_source_id: str
    ) -> list[ResourceData]:
        out: list[ResourceData] = []
        for m in self.client.list_materials(course_source_id):
            if m.get("topicId") != topic_source_id:
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
                    df = mat["driveFile"]
                    link = (df.get("driveFile") or {}).get("alternateLink") or df.get(
                        "alternateLink"
                    )
                    out.append(
                        ResourceData(
                            topic_source_id, sid, "file", df.get("title") or title,
                            raw_url=link,
                            mime_type=df.get("mimeType") or (df.get("driveFile") or {}).get("mimeType"),
                            content_bytes=(f"drive:{link}".encode() if link else None),
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
                t = w.get("dueTime", {})
                due = datetime(
                    d["year"], d.get("month", 1), d.get("day", 1),
                    t.get("hours", 23), t.get("minutes", 59), tzinfo=timezone.utc,
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
