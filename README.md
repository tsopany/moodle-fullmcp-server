# fullmcp-server

MCP server that lets an AI client work with a Moodle site: read courses, and build
courses, quizzes and content. Write tools need the
[`local_fullmcp`](https://github.com/tsopany/moodle-local_fullmcp) plugin installed on
the Moodle site; the read tools and a few writes use Moodle's own web service functions.

## Origin

Forked from [`moodle-mcp-bridge`](https://pypi.org/project/moodle-mcp-bridge/) 1.7.0
(MIT, by its contributors). Changes so far: the package is renamed, and the plugin
function prefix `local_mcpbridge_` is replaced by `local_fullmcp_`, the plugin in the
repo above. The original copyright notice is kept in [LICENSE](LICENSE).

## Tools (v1)

36 tools. Version 1 deliberately has **no tool that reads or changes other people's
personal data** (users, enrolments, grades, submissions, quiz attempts, messages, roles) and
**nothing that deletes courses or users**. The matching Moodle functions are not in the
plugin's service either, so they would be refused by Moodle even if added here. The original
package had 88 tools; the other 52 were removed. Also removed: `download_file`, which sent
the Moodle token to any URL the AI client gave it.

Read tools (always on):

- `server_info`: Health check: version, write-mode, config presence, plugin availability.
- `verify_connection`: Verify the token/URL work and return Moodle site info.
- `list_courses`: List all courses on the site the token can see.
- `search_courses`: Search courses by a single field.
- `get_course_content`: Get the full section/activity structure of one course.
- `list_quizzes`: List quizzes in one or more courses.
- `get_upcoming_events`: Get upcoming calendar events from Moodle, soonest first.
- `list_course_competencies`: List the competencies attached to a course.

Write tools (only with `MOODLE_ALLOW_WRITE=true`):

- `create_course`: Create a new course.
- `create_category`: Create a course category.
- `upload_file`: Upload a file into Moodle's file storage.
- `create_page`: Create a Page activity (HTML) in a course.
- `create_book`: Create a Book activity plus its first chapter.
- `create_label`: Create a Label (inline text/HTML) in a course.
- `create_url`: Create a URL resource in a course.
- `create_quiz`: Create an empty Quiz activity (settings only).
- `add_quiz_question`: Add a multiple-choice question to an existing quiz.
- `add_truefalse_question`: Add a true/false question to a quiz.
- `add_shortanswer_question`: Add a short-answer question to a quiz.
- `add_essay_question`: Add an essay (free-text) question to a quiz.
- `add_numerical_question`: Add a numerical question to a quiz.
- `add_matching_question`: Add a matching question to a quiz.
- `create_forum`: Create a Forum activity in a course.
- `create_choice`: Create a Choice (poll) activity in a course.
- `create_assignment`: Create an Assignment activity in a course.
- `create_section`: Create a new section (topic) in a course.
- `update_activity`: Rename and/or show/hide an existing activity.
- `update_page`: Replace the content of an existing Page activity.
- `move_activity`: Move an activity to a different course section.
- `update_course`: Edit a course's settings.
- `delete_activity`: Delete an activity from a course.
- `delete_section`: Delete a course section.
- `import_course`: Import content from one course into another.
- `backup_course`: Back a whole course up to a .mbz file on the local machine.
- `restore_course`: Restore a .mbz backup file into a NEW course.
- `build_course`: Build a whole course from one JSON outline.

## Use

    pip install .
    MOODLE_URL=https://lms.example.org MOODLE_TOKEN=... fullmcp-server

Write tools are off unless `MOODLE_ALLOW_WRITE=true`. `backup_course` and `restore_course` only
read and write files inside `FULLMCP_FILES_DIR` (default `./fullmcp-files`). The token belongs to a limited
service user, not an admin; see the plugin's `docs/functions.md` for its role.

Status: alpha. Not yet tested against a Moodle site.
