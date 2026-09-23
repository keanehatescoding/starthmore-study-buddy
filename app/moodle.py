"""Moodle web-services client. Token auth, REST/JSON, stdlib only.

Key API facts (easy to get wrong):
- Every call is a POST to /webservice/rest/server.php with
  wstoken + wsfunction + moodlewsrestformat=json.
- Errors come back as HTTP 200 with an {"exception": ...} body.
- File download: the `fileurl` from core_course_get_contents alone serves
  a login page. Append ?token=YOUR_TOKEN (or &token=) to get bytes.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


class MoodleError(RuntimeError):
    pass


class MoodleClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        if not token:
            raise MoodleError("Moodle token is empty — set MOODLE_TOKEN in .env")
        self.base_url = base_url.rstrip("/")
        # Tolerate the token endpoint being pasted as the base URL.
        if self.base_url.endswith("/login/token.php"):
            self.base_url = self.base_url[: -len("/login/token.php")]
        self.token = token
        self.timeout = timeout

    def call(self, function: str, **params):
        """Call a web-service function, return decoded JSON."""
        payload = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            **{k: v for k, v in params.items()},
        }
        data = urllib.parse.urlencode(payload, doseq=True).encode()
        req = urllib.request.Request(
            f"{self.base_url}/webservice/rest/server.php", data=data, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception as e:
            raise MoodleError(f"{function} request failed: {e}") from e
        if isinstance(body, dict) and body.get("exception"):
            raise MoodleError(f"{function}: {body.get('errorcode')}: {body.get('message')}")
        return body

    # -- capability probe: run first, confirms which functions this token may call
    def site_info(self):
        return self.call("core_webservice_get_site_info")

    def get_users_courses(self, userid: int):
        return self.call("core_enrol_get_users_courses", userid=userid)

    def get_course_contents(self, courseid: int):
        return self.call("core_course_get_contents", courseid=courseid)

    def get_assignments(self, *courseids: int):
        params = {f"courseids[{i}]": c for i, c in enumerate(courseids)}
        return self.call("mod_assign_get_assignments", **params)

    def download(self, fileurl: str) -> tuple[bytes, str | None]:
        """Download a fileurl. Returns (bytes, mime_type)."""
        sep = "&" if "?" in fileurl else "?"
        url = f"{fileurl}{sep}token={urllib.parse.quote(self.token)}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                blob = resp.read()
                mime = resp.headers.get_content_type()
        except Exception as e:
            raise MoodleError(f"download failed for {fileurl}: {e}") from e
        if mime == "text/html" and blob.lstrip()[:1] == b"<":
            raise MoodleError(
                f"download returned a login page for {fileurl} — token rejected?"
            )
        return blob, mime


# -- adapter: API responses -> app.sync dataclasses ----------------------------

from app.sync import (  # noqa: E402
    AssignmentData,
    CourseData,
    ResourceData,
    TopicData,
    link_type,
)


class MoodleAdapter:
    """Maps Moodle API shapes onto the normalized sync dataclasses."""

    source = "moodle"

    def __init__(self, client: MoodleClient):
        self.client = client

    def fetch_courses(self) -> list[CourseData]:
        info = self.client.site_info()
        courses = self.client.get_users_courses(info["userid"])
        return [
            CourseData(
                source_id=str(c["id"]),
                name=c.get("fullname") or c.get("shortname", ""),
                code=c.get("shortname"),
            )
            for c in courses
        ]

    def fetch_topics(self, course_source_id: str) -> list[TopicData]:
        sections = self.client.get_course_contents(int(course_source_id))
        topics = []
        for i, s in enumerate(sections):
            name = (s.get("name") or "").strip()
            topics.append(
                TopicData(
                    source_id=str(s["id"]),
                    title=name or f"Section {i}",
                    order=i,
                )
            )
        return topics

    def _page_text(self, course_source_id: str, module_id: int) -> str | None:
        """Best-effort page content. Returns None if the function isn't allowed."""
        try:
            pages = self.client.call(
                "mod_page_get_pages_by_courses",
                **{"courseids[0]": int(course_source_id)},
            )
        except MoodleError:
            return None
        for p in pages.get("pages", []):
            if p.get("coursemodule") == module_id:
                return p.get("content")
        return None

    def fetch_resources(
        self, course_source_id: str, topic_source_id: str
    ) -> list[ResourceData]:
        sections = self.client.get_course_contents(int(course_source_id))
        section = next((s for s in sections if str(s["id"]) == topic_source_id), None)
        if section is None:
            return []
        out: list[ResourceData] = []
        for mod in section.get("modules", []):
            modname = mod.get("modname")
            if modname in ("assign", "label", "forum", "quiz", "feedback", "choice"):
                continue  # not learnable content (assign handled separately)
            if modname == "folder":
                for c in mod.get("contents", []):
                    if c.get("type") != "file":
                        continue
                    blob, mime = self.client.download(c["fileurl"])
                    out.append(
                        ResourceData(
                            topic_source_id=topic_source_id,
                            source_id=f"{mod['id']}:{c.get('filepath', '/')}{c['filename']}",
                            type="file",
                            title=c["filename"],
                            raw_url=c["fileurl"],
                            mime_type=mime or c.get("mimetype"),
                            content_bytes=blob,
                        )
                    )
            elif modname == "url":
                contents = mod.get("contents", [])
                target = contents[0]["fileurl"] if contents else None
                if not target:
                    continue
                out.append(
                    ResourceData(
                        topic_source_id=topic_source_id,
                        source_id=str(mod["id"]),
                        type=link_type(target),
                        title=mod.get("name", target),
                        raw_url=target,
                        content_bytes=target.encode("utf-8"),  # hash the URL
                    )
                )
            elif modname == "page":
                text = self._page_text(course_source_id, mod["id"])
                out.append(
                    ResourceData(
                        topic_source_id=topic_source_id,
                        source_id=str(mod["id"]),
                        type="page_text",
                        title=mod.get("name", "Page"),
                        raw_url=(mod.get("url")),
                        text=text,
                        content_bytes=(
                            None if text is not None
                            else f"page:{mod['id']}".encode()
                        ),
                    )
                )
            elif modname == "resource":
                files = [c for c in mod.get("contents", []) if c.get("type") == "file"]
                if not files:
                    continue
                if len(files) == 1:
                    c = files[0]
                    blob, mime = self.client.download(c["fileurl"])
                    out.append(
                        ResourceData(
                            topic_source_id=topic_source_id,
                            source_id=str(mod["id"]),
                            type="file",
                            title=mod.get("name") or c["filename"],
                            raw_url=c["fileurl"],
                            mime_type=mime or c.get("mimetype"),
                            content_bytes=blob,
                        )
                    )
                else:  # same per-file treatment as folders
                    for c in files:
                        blob, mime = self.client.download(c["fileurl"])
                        out.append(
                            ResourceData(
                                topic_source_id=topic_source_id,
                                source_id=f"{mod['id']}:{c.get('filepath', '/')}{c['filename']}",
                                type="file",
                                title=c["filename"],
                                raw_url=c["fileurl"],
                                mime_type=mime or c.get("mimetype"),
                                content_bytes=blob,
                            )
                        )
            # else: unknown modname — skip silently in v1 (visible via counts)
        return out

    def fetch_assignments(self, course_source_id: str) -> list[AssignmentData]:
        from datetime import datetime, timezone

        sections = self.client.get_course_contents(int(course_source_id))
        cmid_to_section = {
            str(mod["id"]): str(s["id"])
            for s in sections
            for mod in s.get("modules", [])
        }
        resp = self.client.get_assignments(int(course_source_id))
        out: list[AssignmentData] = []
        for course in resp.get("courses", []):
            for a in course.get("assignments", []):
                due = a.get("duedate") or 0
                out.append(
                    AssignmentData(
                        source_id=str(a["id"]),
                        title=a.get("name", "Assignment"),
                        topic_source_id=cmid_to_section.get(str(a.get("cmid"))),
                        due_date=(
                            datetime.fromtimestamp(due, timezone.utc) if due else None
                        ),
                        description=a.get("intro"),
                    )
                )
        return out
