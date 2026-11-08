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

## Use

    pip install .
    MOODLE_URL=https://lms.example.org MOODLE_TOKEN=... fullmcp-server

Write tools are off unless `MOODLE_ALLOW_WRITE=true`. The token belongs to a limited
service user, not an admin; see the plugin's `docs/functions.md` for its role.

Status: alpha. Not yet tested against a Moodle site.
