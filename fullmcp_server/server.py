"""fullmcp-server — an MCP server that exposes a Moodle instance as tools.

Read tools are always registered. Write tools (course/activity creation,
file upload, backup/restore) are only registered when MOODLE_ALLOW_WRITE=true, so a
misconfiguration can never silently expose destructive operations.

Config comes from environment variables only — the token is never logged.

    MOODLE_URL          e.g. https://moodle.example.edu
    MOODLE_TOKEN        a web service token
    MOODLE_ALLOW_WRITE  "true" to enable write tools (default: read-only)

All calls hit:
    {MOODLE_URL}/webservice/rest/server.php
        ?wstoken=...&wsfunction=...&moodlewsrestformat=json
"""

from __future__ import annotations

import asyncio
import hmac
import html as _htmllib
import os
import re
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Configuration (env vars only — never hard-code or log the token).
# Load a .env sitting next to this file so the server works regardless of the
# working directory it is launched from (e.g. by an MCP client). Real env vars
# already set take precedence over the file.
# --------------------------------------------------------------------------- #

try:
    from dotenv import load_dotenv

    # Look for a .env next to this module, in its parent (the repo checkout
    # layout), then the working directory. First value found wins.
    _here = Path(__file__).resolve().parent
    for _candidate in (_here / ".env", _here.parent / ".env", Path.cwd() / ".env"):
        if _candidate.is_file():
            load_dotenv(_candidate)
except ImportError:
    pass

MOODLE_URL = os.environ.get("MOODLE_URL", "").rstrip("/")
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN", "")
ALLOW_WRITE = os.environ.get("MOODLE_ALLOW_WRITE", "").strip().lower() == "true"

REST_ENDPOINT = f"{MOODLE_URL}/webservice/rest/server.php"

# Backups are written to and restored from this folder only, so the AI client cannot make the
# server read or overwrite arbitrary files on this machine.
FILES_DIR = Path(os.environ.get("FULLMCP_FILES_DIR", "fullmcp-files")).resolve()

mcp = FastMCP("fullmcp-server")


class MoodleError(Exception):
    """Raised when Moodle returns an error, even on HTTP 200."""


# --------------------------------------------------------------------------- #
# Shared request helper.
# --------------------------------------------------------------------------- #

async def _call(wsfunction: str, params: dict[str, Any] | None = None) -> Any:
    """Call a Moodle web service function and return parsed JSON.

    Moodle returns errors as a JSON object containing "exception"/"errorcode"
    even with an HTTP 200 status, so we detect and raise those as clean errors.
    The token is passed as a POST field and is never logged.
    """
    if not MOODLE_URL or not MOODLE_TOKEN:
        raise MoodleError(
            "MOODLE_URL and MOODLE_TOKEN must be set in the environment."
        )

    data: dict[str, Any] = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
    }
    if params:
        data.update(_flatten(params))

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(REST_ENDPOINT, data=data)
        resp.raise_for_status()

        # Some functions (e.g. deletes) legitimately return null.
        if not resp.content:
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MoodleError(f"Non-JSON response from Moodle: {resp.text[:300]}") from exc

    if isinstance(payload, dict) and "exception" in payload:
        code = payload.get("errorcode", "unknown")
        message = payload.get("message", "Unknown Moodle error")
        debug = payload.get("debuginfo")
        detail = f" ({debug})" if debug else ""
        raise MoodleError(f"Moodle error [{code}]: {message}{detail}")

    return payload


def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested dict/list params into Moodle's PHP-style form encoding.

    Moodle expects e.g. courses[0][fullname]=... rather than JSON bodies.
    """
    flat: dict[str, str] = {}
    for key, value in params.items():
        composite = f"{prefix}[{key}]" if prefix else str(key)
        _flatten_value(composite, value, flat)
    return flat


def _flatten_value(key: str, value: Any, flat: dict[str, str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten_value(f"{key}[{k}]", v, flat)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _flatten_value(f"{key}[{i}]", item, flat)
    elif isinstance(value, bool):
        flat[key] = "1" if value else "0"
    elif value is not None:
        flat[key] = str(value)


# --------------------------------------------------------------------------- #
# build_course outline validation (pure — no network, unit-testable).
# --------------------------------------------------------------------------- #

_ACTIVITY_TYPES = {
    "page", "label", "url", "forum", "assignment", "choice", "quiz",
}
_QUESTION_TYPES = {
    "multichoice", "truefalse", "shortanswer", "essay", "numerical", "matching",
}


def _validate_outline(outline: Any) -> list[str]:
    """Return a list of human-readable problems with a build_course outline.

    Empty list means the outline is well-formed. Runs before any writes so a
    malformed outline fails fast with a clear message instead of half-building.
    """
    errors: list[str] = []
    if not isinstance(outline, dict):
        return ["outline must be a JSON object"]

    if not outline.get("courseid"):
        for field in ("fullname", "shortname"):
            val = outline.get(field)
            if not isinstance(val, str) or not val.strip():
                errors.append(f"outline.{field} is required (non-empty) when courseid is not given")

    sections = outline.get("sections", [])
    if not isinstance(sections, list):
        return errors + ["outline.sections must be a list"]

    for si, sec in enumerate(sections):
        where = f"sections[{si}]"
        if not isinstance(sec, dict):
            errors.append(f"{where} must be an object")
            continue
        acts = sec.get("activities", [])
        if not isinstance(acts, list):
            errors.append(f"{where}.activities must be a list")
            continue
        for ai, act in enumerate(acts):
            aw = f"{where}.activities[{ai}]"
            if not isinstance(act, dict):
                errors.append(f"{aw} must be an object")
                continue
            atype = act.get("type")
            if atype not in _ACTIVITY_TYPES:
                errors.append(
                    f"{aw}.type {atype!r} is not one of {sorted(_ACTIVITY_TYPES)}")
                continue
            if atype == "quiz":
                for qi, q in enumerate(act.get("questions", []) or []):
                    qw = f"{aw}.questions[{qi}]"
                    if not isinstance(q, dict):
                        errors.append(f"{qw} must be an object")
                    elif q.get("qtype", "multichoice") not in _QUESTION_TYPES:
                        errors.append(
                            f"{qw}.qtype {q.get('qtype')!r} is not one of "
                            f"{sorted(_QUESTION_TYPES)}")
    return errors


# --------------------------------------------------------------------------- #
# READ TOOLS (always registered).
# --------------------------------------------------------------------------- #

SERVER_VERSION = "0.1.1"


@mcp.tool()
async def server_info() -> dict:
    """Health check: version, write-mode, config presence, plugin availability.

    Read-only and cheap. Confirms the MCP server is wired up correctly before
    you rely on it: reports this server's version, whether write tools are
    enabled, whether URL/token are configured, and (via a live site-info call)
    whether the local_fullmcp plugin's functions are present on the Moodle
    site. Never returns the token itself.
    """
    info: dict[str, Any] = {
        "server_version": SERVER_VERSION,
        "write_enabled": ALLOW_WRITE,
        "moodle_url_set": bool(MOODLE_URL),
        "moodle_token_set": bool(MOODLE_TOKEN),
    }
    try:
        site = await _call("core_webservice_get_site_info")
        funcs = {f.get("name") for f in site.get("functions", [])}
        info.update({
            "connected": True,
            "sitename": site.get("sitename"),
            "moodle_release": site.get("release"),
            "plugin_installed": any(f.startswith("local_fullmcp_") for f in funcs),
            "function_count": len(funcs),
        })
    except MoodleError as exc:
        info.update({"connected": False, "error": str(exc)})
    return info


@mcp.tool()
async def verify_connection() -> dict:
    """Verify the token/URL work and return Moodle site info.

    Read-only. Call this first to confirm connectivity and see which site,
    version, and user the token is bound to.
    """
    return await _call("core_webservice_get_site_info")


@mcp.tool()
async def list_courses() -> list:
    """List all courses on the site the token can see.

    Read-only. Returns id, fullname, shortname, categoryid, summary, etc.
    """
    return await _call("core_course_get_courses")


@mcp.tool()
async def search_courses(field: str, value: str) -> dict:
    """Search courses by a single field.

    Read-only. `field` is one of: id, ids, shortname, idnumber, category.
    Example: field="shortname", value="CS101".
    """
    return await _call("core_course_get_courses_by_field", {"field": field, "value": value})


@mcp.tool()
async def get_course_content(courseid: int) -> list:
    """Get the full section/activity structure of one course.

    Read-only. Returns sections, each with the modules (activities/resources)
    inside it — useful for seeing what already exists before adding content.
    """
    return await _call("core_course_get_contents", {"courseid": courseid})


@mcp.tool()
async def list_quizzes(courseids: list[int]) -> dict:
    """List quizzes in one or more courses.

    Read-only. Pass a list of course IDs.
    """
    return await _call("mod_quiz_get_quizzes_by_courses", {"courseids": courseids})


# --------------------------------------------------------------------------- #
# STUDENT / ANALYSIS READ TOOLS (always registered).
#
# These build on the raw web service functions above to give an AI assistant a
# student's-eye view: courses, assignments, deadlines, grades, progress, and
# combined dashboards. The "analysis" tools gather and structure the relevant
# Moodle data; the calling AI does the natural-language reasoning over it.
#
# They rely on extra web service functions being present in the token's service:
#   core_enrol_get_users_courses, mod_assign_get_assignments,
#   mod_assign_get_submission_status, gradereport_overview_get_course_grades,
#   core_calendar_get_action_events_by_timesort, mod_forum_get_forums_by_courses,
#   mod_forum_get_forum_discussions, core_completion_get_activities_completion_status
# --------------------------------------------------------------------------- #

_STOPWORDS = {
    "the", "and", "for", "you", "your", "this", "that", "with", "from", "have",
    "will", "are", "was", "not", "but", "all", "can", "our", "out", "use", "any",
    "how", "who", "why", "what", "when", "which", "into", "than", "then", "them",
    "should", "must", "may", "shall", "each", "also", "using", "used", "one", "two",
    "assignment", "assignments", "task", "please", "student", "students", "submit",
}


def _now() -> int:
    return int(_time.time())


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = _htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_USERID_CACHE: dict[str, int] = {}


# --- Course & content ------------------------------------------------------ #

# --- Assignments & deadlines ----------------------------------------------- #

# --- Grades & progress ----------------------------------------------------- #

# --- Aggregated overviews -------------------------------------------------- #

@mcp.tool()
async def get_upcoming_events(limit: int = 20) -> list:
    """Get upcoming calendar events from Moodle, soonest first.

    Read-only. Uses Moodle's action-events calendar view from now onward.
    """
    data = await _call(
        "core_calendar_get_action_events_by_timesort",
        {"timesortfrom": _now(), "limitnum": limit},
    )
    events = data.get("events", []) if isinstance(data, dict) else []
    return [
        {
            "name": e.get("name"),
            "timestart": _iso(e.get("timestart")),
            "timesort": e.get("timesort"),
            "courseid": (e.get("course") or {}).get("id"),
            "url": (e.get("action") or {}).get("url") or e.get("url"),
        }
        for e in events
    ]


# --- Quiz results ----------------------------------------------------------- #

# --- Completion tracking ---------------------------------------------------- #

# --- Notifications ---------------------------------------------------------- #

# --- Competencies & learning plans ------------------------------------------ #

@mcp.tool()
async def list_course_competencies(courseid: int) -> list:
    """List the competencies attached to a course.

    Read-only. Returns each competency's id, name, description and id number.
    Empty list if the course uses no competencies.
    """
    data = await _call(
        "core_competency_list_course_competencies", {"id": courseid}
    )
    return [
        {
            "competencyid": (c.get("competency") or {}).get("id"),
            "shortname": (c.get("competency") or {}).get("shortname"),
            "idnumber": (c.get("competency") or {}).get("idnumber"),
            "description": _strip_html((c.get("competency") or {}).get("description")),
        }
        for c in data
    ]


# --------------------------------------------------------------------------- #
# WRITE TOOLS (registered only when MOODLE_ALLOW_WRITE=true).
# --------------------------------------------------------------------------- #

def _files_path(name: str) -> Path:
    """Resolve `name` inside FILES_DIR; refuse anything that escapes it."""
    target = (FILES_DIR / name).resolve()
    if not target.is_relative_to(FILES_DIR):
        raise MoodleError(f"Path must be inside {FILES_DIR} (set FULLMCP_FILES_DIR to change it).")
    return target


def _register_write_tools() -> None:
    """Register write tools. Called only when writes are enabled."""

    # --- Core writes ------------------------------------------------------- #

    @mcp.tool()
    async def create_course(
        fullname: str,
        shortname: str,
        categoryid: int = 1,
        summary: str = "",
        visible: int = 1,
    ) -> list:
        """⚠️ WRITES LIVE DATA. Create a new course.

        Creates a real course on the Moodle site. `shortname` must be unique.
        `categoryid` must be an existing category (1 is usually the default).
        Returns the new course id and shortname.
        """
        course = {
            "fullname": fullname,
            "shortname": shortname,
            "categoryid": categoryid,
            "summary": summary,
            "summaryformat": 1,
            "visible": visible,
        }
        return await _call("core_course_create_courses", {"courses": [course]})

    @mcp.tool()
    async def create_category(name: str, parent: int = 0, description: str = "") -> list:
        """⚠️ WRITES LIVE DATA. Create a course category.

        `parent` 0 = top level. Returns the new category id and name.
        """
        category = {
            "name": name,
            "parent": parent,
            "description": description,
            "descriptionformat": 1,
        }
        return await _call("core_course_create_categories", {"categories": [category]})

    @mcp.tool()
    async def upload_file(
        filename: str,
        filecontent_base64: str,
        contextid: int = 0,
        component: str = "user",
        filearea: str = "draft",
        itemid: int = 0,
        filepath: str = "/",
    ) -> Any:
        """⚠️ WRITES LIVE DATA. Upload a file into Moodle's file storage.

        `filecontent_base64` is the base64-encoded file bytes. By default the
        file lands in the calling user's draft area (component=user,
        filearea=draft) — the returned itemid can then be attached to an
        activity. Returns the stored file descriptor(s).
        """
        params = {
            "contextid": contextid or 0,
            "component": component,
            "filearea": filearea,
            "itemid": itemid,
            "filepath": filepath,
            "filename": filename,
            "filecontent": filecontent_base64,
        }
        return await _call("core_files_upload", params)

    # --- Plugin writes (local_fullmcp) ----------------------------------- #

    @mcp.tool()
    async def create_page(
        courseid: int,
        name: str,
        content: str,
        section: int = 0,
        intro: str = "",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Page activity (HTML) in a course.

        Requires the local_fullmcp plugin on the Moodle site. `content` is the
        page body (HTML). Returns the new cmid and page instance id.
        """
        return await _call(
            "local_fullmcp_create_page",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "content": content,
                "intro": intro,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def create_book(
        courseid: int,
        name: str,
        chaptertitle: str,
        chaptercontent: str,
        section: int = 0,
        intro: str = "",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Book activity plus its first chapter.

        Requires the local_fullmcp plugin. Returns cmid, book instance id, and
        the first chapter id.
        """
        return await _call(
            "local_fullmcp_create_book",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "chaptertitle": chaptertitle,
                "chaptercontent": chaptercontent,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def create_label(
        courseid: int,
        content: str,
        section: int = 0,
        name: str = "",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Label (inline text/HTML) in a course.

        Requires the local_fullmcp plugin. Returns the new cmid and instance id.
        """
        return await _call(
            "local_fullmcp_create_label",
            {
                "courseid": courseid,
                "section": section,
                "content": content,
                "name": name,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def create_url(
        courseid: int,
        name: str,
        externalurl: str,
        section: int = 0,
        intro: str = "",
        display: int = 0,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a URL resource in a course.

        Requires the local_fullmcp plugin. Returns the new cmid and instance id.
        """
        return await _call(
            "local_fullmcp_create_url",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "externalurl": externalurl,
                "intro": intro,
                "display": display,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def create_quiz(
        courseid: int,
        name: str,
        section: int = 0,
        intro: str = "",
        timeopen: int = 0,
        timeclose: int = 0,
        timelimit: int = 0,
        grade: float = 100.0,
        attempts: int = 0,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create an empty Quiz activity (settings only).

        Requires the local_fullmcp plugin. This creates the quiz container;
        adding questions is a separate, heavier operation (not supported here).
        Returns the new cmid and quiz instance id.
        """
        return await _call(
            "local_fullmcp_create_quiz",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "timeopen": timeopen,
                "timeclose": timeclose,
                "timelimit": timelimit,
                "grade": grade,
                "attempts": attempts,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def add_quiz_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        answers: list[dict],
        single: int = 1,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a multiple-choice question to an existing quiz.

        Requires the local_fullmcp plugin. `quizcmid` is the quiz's course
        module id (the cmid returned by create_quiz). `answers` is a list of
        dicts, each: {"text": "...", "fraction": 1.0, "feedback": ""} where
        fraction is the grade weight (1.0 = fully correct, 0 = wrong, negatives
        penalise). Provide at least two answers. `single=1` means exactly one
        correct answer. Returns the new question id and its slot in the quiz.
        """
        return await _call(
            "local_fullmcp_add_quiz_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "answers": answers,
                "single": single,
                "defaultmark": defaultmark,
            },
        )

    @mcp.tool()
    async def add_truefalse_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        correctanswer: int,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a true/false question to a quiz.

        Requires the local_fullmcp plugin. `quizcmid` is the quiz's course
        module id. `correctanswer` = 1 for true, 0 for false. Returns the new
        question id and its slot.
        """
        return await _call(
            "local_fullmcp_add_truefalse_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "correctanswer": correctanswer,
                "defaultmark": defaultmark,
            },
        )

    @mcp.tool()
    async def add_shortanswer_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        answers: list[str],
        usecase: int = 0,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a short-answer question to a quiz.

        Requires the local_fullmcp plugin. `answers` is a list of accepted
        answer strings (all treated as fully correct). `usecase` = 1 for
        case-sensitive matching. Returns the new question id and its slot.
        """
        return await _call(
            "local_fullmcp_add_shortanswer_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "answers": answers,
                "usecase": usecase,
                "defaultmark": defaultmark,
            },
        )

    @mcp.tool()
    async def add_essay_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        graderinfo: str = "",
        responsetemplate: str = "",
        responselines: int = 15,
        attachments: int = 0,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add an essay (free-text) question to a quiz.

        Requires the local_fullmcp plugin. Essay questions are graded
        manually. `graderinfo` is private guidance for whoever marks it;
        `responsetemplate` pre-fills the student's answer box; `attachments`
        allows file uploads (0 = none, 1-3 = that many, -1 = unlimited).
        Returns the new question id and its slot.
        """
        return await _call(
            "local_fullmcp_add_essay_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "graderinfo": graderinfo,
                "responsetemplate": responsetemplate,
                "responselines": responselines,
                "attachments": attachments,
                "defaultmark": defaultmark,
            },
        )

    @mcp.tool()
    async def add_numerical_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        answers: list[dict],
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a numerical question to a quiz.

        Requires the local_fullmcp plugin. `answers` is a list of dicts,
        each: {"answer": 42.0, "tolerance": 0.5, "fraction": 1.0,
        "feedback": ""} — a student response within ±tolerance of answer
        earns that fraction of the marks (1.0 = full credit). Returns the new
        question id and its slot.
        """
        return await _call(
            "local_fullmcp_add_numerical_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "answers": answers,
                "defaultmark": defaultmark,
            },
        )

    @mcp.tool()
    async def add_matching_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        pairs: list[dict],
        shuffleanswers: int = 1,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a matching question to a quiz.

        Requires the local_fullmcp plugin. `pairs` is a list of dicts, each:
        {"question": "Capital of France", "answer": "Paris"} — students match
        each left-hand item to the right answer. At least two pairs. Returns
        the new question id and its slot.
        """
        return await _call(
            "local_fullmcp_add_matching_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "pairs": pairs,
                "shuffleanswers": shuffleanswers,
                "defaultmark": defaultmark,
            },
        )

    @mcp.tool()
    async def create_forum(
        courseid: int,
        name: str,
        section: int = 0,
        intro: str = "",
        type: str = "general",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Forum activity in a course.

        Requires the local_fullmcp plugin. `type` is one of: general,
        eachuser, single, qanda, blog. Returns the new cmid and instance id.
        """
        return await _call(
            "local_fullmcp_create_forum",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "type": type,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def create_choice(
        courseid: int,
        name: str,
        question: str,
        options: list[str],
        section: int = 0,
        allowupdate: int = 1,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Choice (poll) activity in a course.

        Requires the local_fullmcp plugin. `options` is a list of at least two
        answer strings students pick from. Returns the new cmid and instance id.
        """
        return await _call(
            "local_fullmcp_create_choice",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "question": question,
                "options": options,
                "allowupdate": allowupdate,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def create_assignment(
        courseid: int,
        name: str,
        section: int = 0,
        intro: str = "",
        duedate: int = 0,
        grade: int = 100,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create an Assignment activity in a course.

        Requires the local_fullmcp plugin. Enables online-text and file
        submissions with feedback comments. `duedate` is a unix timestamp
        (0 = no due date). Returns the new cmid and instance id.
        """
        return await _call(
            "local_fullmcp_create_assignment",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "duedate": duedate,
                "grade": grade,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def create_section(
        courseid: int,
        name: str,
        summary: str = "",
        position: int = 0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a new section (topic) in a course.

        Requires the local_fullmcp plugin. `position` 0 appends to the end.
        Returns the new section id and its number within the course.
        """
        return await _call(
            "local_fullmcp_create_section",
            {
                "courseid": courseid,
                "name": name,
                "summary": summary,
                "position": position,
            },
        )

    @mcp.tool()
    async def update_section(
        courseid: int,
        sectionnum: int,
        name: str = "",
        summary: str = "",
        visible: int = -1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Rename, re-describe and/or show/hide a course section.

        Requires the local_fullmcp plugin. `sectionnum` is the section number
        in the course. Leave `name` or `summary` empty to keep them; `visible`
        = 1 show, 0 hide, -1 leave unchanged. Returns the section id and number.
        """
        return await _call(
            "local_fullmcp_update_section",
            {
                "courseid": courseid,
                "sectionnum": sectionnum,
                "name": name,
                "summary": summary,
                "visible": visible,
            },
        )

    @mcp.tool()
    async def update_activity(cmid: int, name: str = "", visible: int = -1) -> dict:
        """⚠️ WRITES LIVE DATA. Rename and/or show/hide an existing activity.

        Requires the local_fullmcp plugin. `cmid` is the activity's course
        module id. Leave `name` empty to keep it; `visible` = 1 show, 0 hide,
        -1 leave unchanged. Returns what was changed.
        """
        return await _call(
            "local_fullmcp_update_activity",
            {"cmid": cmid, "name": name, "visible": visible},
        )

    @mcp.tool()
    async def update_page(cmid: int, content: str, intro: str = "") -> dict:
        """⚠️ WRITES LIVE DATA. Replace the content of an existing Page activity.

        Requires the local_fullmcp plugin. `cmid` is the page's course module
        id; `content` is the new HTML body. Leave `intro` empty to keep the
        current description. Returns the cmid and a success flag.
        """
        return await _call(
            "local_fullmcp_update_page",
            {"cmid": cmid, "content": content, "intro": intro},
        )

    @mcp.tool()
    async def move_activity(cmid: int, section: int) -> dict:
        """⚠️ WRITES LIVE DATA. Move an activity to a different course section.

        Requires the local_fullmcp plugin. `cmid` is the activity's course
        module id; `section` is the target section number. Returns the cmid and
        the section it now lives in.
        """
        return await _call(
            "local_fullmcp_move_activity",
            {"cmid": cmid, "section": section},
        )

    @mcp.tool()
    async def update_course(
        courseid: int,
        fullname: str = "",
        shortname: str = "",
        summary: str = "",
        visible: int = -1,
    ) -> list:
        """⚠️ WRITES LIVE DATA. Edit a course's settings.

        Only the fields you provide are changed: leave `fullname`/`shortname`/
        `summary` empty to keep them, and `visible` = 1 show, 0 hide, -1 leave.
        Returns the update result.
        """
        course: dict[str, Any] = {"id": courseid}
        if fullname:
            course["fullname"] = fullname
        if shortname:
            course["shortname"] = shortname
        if summary:
            course["summary"] = summary
            course["summaryformat"] = 1
        if visible in (0, 1):
            course["visible"] = visible
        return await _call("core_course_update_courses", {"courses": [course]})

    @mcp.tool()
    async def delete_activity(cmid: int) -> dict:
        """⚠️ WRITES LIVE DATA — IRREVERSIBLE. Delete an activity from a course.

        Requires the local_fullmcp plugin. `cmid` is the activity's course
        module id. This permanently removes the activity and its data. Returns
        the deleted cmid and a success flag.
        """
        return await _call("local_fullmcp_delete_activity", {"cmid": cmid})

    @mcp.tool()
    async def delete_section(courseid: int, sectionnum: int) -> dict:
        """⚠️ WRITES LIVE DATA — IRREVERSIBLE. Delete a course section.

        Requires the local_fullmcp plugin. `sectionnum` is the section number
        (1 or higher — the general section 0 cannot be deleted). This removes the
        section AND every activity inside it. Returns a success flag.
        """
        return await _call(
            "local_fullmcp_delete_section",
            {"courseid": courseid, "sectionnum": sectionnum},
        )

    # --- Extended core writes (Moodle core web services) ------------------- #

    @mcp.tool()
    async def import_course(importfrom: int, importto: int, deletecontent: int = 0) -> None:
        """⚠️ WRITES LIVE DATA. Import content from one course into another.

        Uses core_course_import_course. Copies activities/resources from
        `importfrom` into `importto`. `deletecontent` 1 wipes the target first.
        """
        return await _call(
            "core_course_import_course",
            {"importfrom": importfrom, "importto": importto, "deletecontent": deletecontent},
        )

    @mcp.tool()
    async def backup_course(
        courseid: int, save_to: str = "", includeusers: int = 0
    ) -> dict:
        """Back a whole course up to a .mbz file on the local machine.

        Requires the local_fullmcp plugin. The backup runs on the Moodle
        server, is transferred base64-encoded, and is written to `save_to`, a
        path inside the server's files folder (FULLMCP_FILES_DIR, default
        ./fullmcp-files; default name course_<id>.mbz). Content-only by default; pass
        includeusers=1 to keep enrolments and user data. Courses over 100MB
        are refused. Returns the saved path and size.
        """
        import base64 as _b64

        result = await _call(
            "local_fullmcp_backup_course",
            {"courseid": courseid, "includeusers": includeusers},
        )
        path = _files_path(save_to or f"course_{courseid}.mbz")
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, _b64.b64decode(result["content_base64"]))
        return {
            "saved_to": os.path.abspath(path),
            "size": result["size"],
            "filename": result["filename"],
        }

    @mcp.tool()
    async def restore_course(
        filepath: str,
        categoryid: int = 1,
        fullname: str = "",
        shortname: str = "",
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Restore a .mbz backup file into a NEW course.

        Requires the local_fullmcp plugin. Reads `filepath`, a path inside the server's files folder (FULLMCP_FILES_DIR),
        (created by backup_course, or any Moodle .mbz), uploads it and
        restores it as a brand-new course in `categoryid`. `fullname` and
        `shortname` override the names stored in the backup. Returns the new
        course's id and names.
        """
        import base64 as _b64

        content = await asyncio.to_thread(_files_path(filepath).read_bytes)
        return await _call(
            "local_fullmcp_restore_course",
            {
                "content_base64": _b64.b64encode(content).decode("ascii"),
                "categoryid": categoryid,
                "fullname": fullname,
                "shortname": shortname,
            },
        )

    @mcp.tool()
    async def build_course(outline: dict, stop_on_error: bool = False) -> dict:
        """⚠️ WRITES LIVE DATA. Build a whole course from one JSON outline.

        The outline is validated up front — a malformed one is rejected before
        anything is written, with a list of problems. Set `stop_on_error=True`
        to stop at the first section or activity that fails; what was built up
        to then is kept (this server cannot delete courses, so clean up in
        Moodle). With `stop_on_error=False` (default) a failing item is recorded
        in the log and the rest still build.

        One call instead of many. `outline` shape:
        {
          "fullname": "Biology 101", "shortname": "bio101", "categoryid": 1,
          "summary": "...",            # optional
          "courseid": 0,               # optional: build into an existing course
          "sections": [
            {"name": "Week 1", "summary": "...",
             "activities": [
               {"type": "page", "name": "Welcome", "content": "<p>Hi</p>"},
               {"type": "label", "text": "<b>Read this first</b>"},
               {"type": "url", "name": "Syllabus", "url": "https://..."},
               {"type": "forum", "name": "Q&A", "intro": "..."},
               {"type": "assignment", "name": "Report", "intro": "...",
                "duedate": 0},
               {"type": "quiz", "name": "Checkpoint", "intro": "...",
                "questions": [
                  {"qtype": "multichoice", "name": "Q1", "text": "...",
                   "answers": [{"text": "A", "fraction": 1.0},
                                {"text": "B", "fraction": 0.0}]},
                  {"qtype": "truefalse", "name": "Q2", "text": "...",
                   "correct": 1},
                  {"qtype": "shortanswer", "name": "Q3", "text": "...",
                   "answers": ["Paris"]},
                  {"qtype": "essay", "name": "Q4", "text": "..."},
                  {"qtype": "numerical", "name": "Q5", "text": "...",
                   "answers": [{"answer": 42, "tolerance": 0.5}]},
                  {"qtype": "matching", "name": "Q6", "text": "...",
                   "pairs": [{"question": "France", "answer": "Paris"},
                              {"question": "Italy", "answer": "Rome"}]}
                ]}
             ]}
          ]
        }
        Creates the course (unless `courseid` is given), then every section
        and activity in order. Returns the course id plus a per-item build
        log; one failing activity is reported but does not stop the rest.
        """
        problems = _validate_outline(outline)
        if problems:
            raise MoodleError("Invalid course outline: " + "; ".join(problems))

        log: list[dict] = []

        courseid = int(outline.get("courseid") or 0)
        if not courseid:
            created = await _call(
                "core_course_create_courses",
                {
                    "courses": [
                        {
                            "fullname": outline["fullname"],
                            "shortname": outline["shortname"],
                            "categoryid": outline.get("categoryid", 1),
                            "summary": outline.get("summary", ""),
                            "summaryformat": 1,
                            "format": "topics",
                        }
                    ]
                },
            )
            courseid = created[0]["id"]
            log.append({"created": "course", "id": courseid})

        async def build_activity(sectionnum: int, act: dict) -> None:
            atype = act.get("type", "")
            name = act.get("name", "")
            if atype == "page":
                r = await _call(
                    "local_fullmcp_create_page",
                    {
                        "courseid": courseid,
                        "name": name,
                        "content": act.get("content", ""),
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                    },
                )
            elif atype == "label":
                r = await _call(
                    "local_fullmcp_create_label",
                    {
                        "courseid": courseid,
                        "content": act.get("text", act.get("content", "")),
                        "section": sectionnum,
                    },
                )
            elif atype == "url":
                r = await _call(
                    "local_fullmcp_create_url",
                    {
                        "courseid": courseid,
                        "name": name,
                        "externalurl": act.get("url", ""),
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                    },
                )
            elif atype == "forum":
                r = await _call(
                    "local_fullmcp_create_forum",
                    {
                        "courseid": courseid,
                        "name": name,
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                        "type": act.get("forumtype", "general"),
                    },
                )
            elif atype == "assignment":
                r = await _call(
                    "local_fullmcp_create_assignment",
                    {
                        "courseid": courseid,
                        "name": name,
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                        "duedate": act.get("duedate", 0),
                        "grade": act.get("grade", 100),
                    },
                )
            elif atype == "choice":
                r = await _call(
                    "local_fullmcp_create_choice",
                    {
                        "courseid": courseid,
                        "name": name,
                        "question": act.get("question", name),
                        "options": act.get("options", []),
                        "section": sectionnum,
                    },
                )
            elif atype == "quiz":
                r = await _call(
                    "local_fullmcp_create_quiz",
                    {
                        "courseid": courseid,
                        "name": name,
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                    },
                )
                quizcmid = r.get("cmid")
                for q in act.get("questions", []):
                    await build_question(quizcmid, q)
            else:
                raise MoodleError(f"Unknown activity type: {atype!r}")
            log.append({"created": atype or "?", "name": name, "result": r})

        async def build_question(quizcmid: int, q: dict) -> None:
            qtype = q.get("qtype", "multichoice")
            common = {
                "quizcmid": quizcmid,
                "name": q.get("name", "Question"),
                "questiontext": q.get("text", ""),
                "defaultmark": q.get("defaultmark", 1.0),
            }
            if qtype == "multichoice":
                await _call(
                    "local_fullmcp_add_quiz_question",
                    {**common, "answers": q.get("answers", []),
                     "single": q.get("single", 1)},
                )
            elif qtype == "truefalse":
                await _call(
                    "local_fullmcp_add_truefalse_question",
                    {**common, "correctanswer": q.get("correct", 1)},
                )
            elif qtype == "shortanswer":
                await _call(
                    "local_fullmcp_add_shortanswer_question",
                    {**common, "answers": q.get("answers", []),
                     "usecase": q.get("usecase", 0)},
                )
            elif qtype == "essay":
                await _call(
                    "local_fullmcp_add_essay_question",
                    {**common, "graderinfo": q.get("graderinfo", "")},
                )
            elif qtype == "numerical":
                await _call(
                    "local_fullmcp_add_numerical_question",
                    {**common, "answers": q.get("answers", [])},
                )
            elif qtype == "matching":
                await _call(
                    "local_fullmcp_add_matching_question",
                    {**common, "pairs": q.get("pairs", [])},
                )
            else:
                raise MoodleError(f"Unknown question type: {qtype!r}")

        async def do_build() -> None:
            for i, sec in enumerate(outline.get("sections", []), start=1):
                r = await _call(
                    "local_fullmcp_create_section",
                    {
                        "courseid": courseid,
                        "name": sec.get("name", f"Section {i}"),
                        "summary": sec.get("summary", ""),
                    },
                )
                sectionnum = r.get("sectionnum", i)
                log.append({"created": "section", "name": sec.get("name"),
                            "sectionnum": sectionnum})
                for act in sec.get("activities", []):
                    try:
                        await build_activity(sectionnum, act)
                    except (MoodleError, httpx.HTTPError) as exc:
                        entry = {
                            "error": str(exc),
                            "activity": act.get("name", act.get("type", "?")),
                        }
                        log.append(entry)
                        if stop_on_error:
                            raise MoodleError(entry["error"]) from exc

        if stop_on_error:
            try:
                await do_build()
            except (MoodleError, httpx.HTTPError) as exc:
                raise MoodleError(
                    f"build_course stopped (course {courseid} kept with what was built so far): {exc}"
                ) from exc
        else:
            await do_build()

        return {"courseid": courseid, "built": log,
                "ok": not any("error" in e for e in log)}


if ALLOW_WRITE:
    _register_write_tools()


class _BearerAuth:
    """ASGI wrapper: every HTTP request needs `Authorization: Bearer <token>`, except GET /health.

    A stopgap for v1 (one shared secret). Real MCP auth is OAuth 2.1: replace this with
    FastMCP's `token_verifier` + `auth` settings once an authorization server exists.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "GET" and scope["path"] == "/health":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        header = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, given = header.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(given, self.token):
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        await self.app(scope, receive, send)


def _run_http() -> None:
    """Serve MCP over streamable HTTP at /mcp (FULLMCP_TRANSPORT=http)."""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    token = os.environ.get("FULLMCP_BEARER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("FULLMCP_BEARER_TOKEN must be set (at least 32 characters) for HTTP mode.")
    host = os.environ.get("FULLMCP_HOST", "127.0.0.1")
    port = int(os.environ.get("FULLMCP_PORT", "8000"))
    # Host-header check: FastMCP only allows localhost unless told the public host name(s).
    hosts = [h.strip() for h in os.environ.get("FULLMCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    mcp.settings.host, mcp.settings.port = host, port
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(hosts),
        allowed_hosts=hosts + [f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[],
    )
    uvicorn.run(_BearerAuth(mcp.streamable_http_app(), token), host=host, port=port, log_level="info")


def main() -> None:
    """Entry point: stdio by default, streamable HTTP with FULLMCP_TRANSPORT=http."""
    transport = os.environ.get("FULLMCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    elif transport == "http":
        _run_http()
    else:
        raise SystemExit(f"FULLMCP_TRANSPORT must be 'stdio' or 'http', not {transport!r}")


if __name__ == "__main__":
    main()
