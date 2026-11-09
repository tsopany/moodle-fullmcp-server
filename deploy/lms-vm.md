# Deploying fullmcp-server next to Moodle on the VM (runbook for STG and PRD)

Everything needed to reproduce the setup that runs on STG (`lms.tsopany.com`, MCP at
`lmsmcp.tsopany.com`). It was done and verified on STG on 2026-11-09. For PRD, follow the same
sections and use the substitutions below.

The pieces:

| Piece | Repo | What it is |
|---|---|---|
| Moodle plugin `local_fullmcp` | `moodle-local_fullmcp` | adds the web service functions and the "Full MCP Service" |
| MCP server | `moodle-fullmcp-server` | Python server; tools call Moodle's REST API; MCP over HTTP at `/mcp` |
| Deployment files | `tsopany-vm-setup` (`lms/`, `caddy/`) | compose template, secrets, Caddy, GitHub Actions deploy |

```
client (agent) --HTTPS--> Caddy (lmsmcp host) --> fullmcp container --HTTPS--> Moodle (lms host)
                                                                         (REST, token of the limited user)
```

## Substitutions for PRD

| STG value | PRD value (fill in) |
|---|---|
| Moodle site `https://lms.tsopany.com` (= Moodle `SITE_URL`) | |
| MCP host `lmsmcp.tsopany.com` | |
| Infisical project `lms`, env `stg` | env `prd` (check `secrets.sh`, it hardcodes `--env=stg`) |
| compose stack `lms`, containers `lms-lms-1`, `lms-fullmcp-1` | |
| volume paths `/var/deploy/lms/...` | |

`MOODLE_URL` must equal the exact `SITE_URL` of that Moodle. Moodle redirects (HTTP 303) any request
with another host name, so internal names such as `http://lms:8080` do not work.

## Versions used on STG

- Plugin `local_fullmcp` **v0.1.2** (v0.1.0 lacks the 14 core functions in the service; most tools fail).
- Image `ghcr.io/tsopany/moodle-fullmcp-server:0.1.3` (0.1.2 and older get HTTP 403 from Cloudflare,
  see "Cloudflare"). The package is public: org setting "Package creation > Public" must be on, then
  the package visibility set to Public. Otherwise the VM needs `docker login ghcr.io` with a
  `read:packages` token.
- Moodle image `erseco/alpine-moodle:latest` (5.3.0 when tested). Consider pinning a tag for PRD, so
  an unreviewed Moodle upgrade does not happen on redeploy. The plugin was tested on Moodle 5.3 only.
- Releases: tag `vX.Y.Z` in each repo. The plugin's CI runs on push and tags. The server's `image`
  workflow builds and pushes the image on `v*` tags (amd64 and arm64).

## How deploys work

`tsopany-vm-setup` deploys with GitHub Actions. A push to `main` touching `lms/**` or `caddy/**` runs
`Deploy lms` / `Deploy caddy`: upload files, run `secrets.sh` (renders the compose template from
Infisical), `docker compose up -d --force-recreate`. Redeploy without a file change (for example
after changing a secret in Infisical):

```bash
cd tsopany-vm-setup && gh workflow run deploy-lms.yml -f reason="why"
gh run list --workflow=deploy-lms.yml --limit 1 --json status,conclusion
```

Order matters: the image tag in the compose file must exist before it is pushed (check the server's
`image` workflow is green first), and the Moodle token only exists after sections 1 and 2.

## 1. Files and VM changes (once)

1. Plugin: clone it on the VM and mount it into the Moodle container. The Moodle image's own
   `PLUGINS=` installer does not work in 5.3 (it copies to `/var/www/html/local/`, but 5.3 serves
   from `/var/www/html/public/`).
   ```bash
   sudo git clone --branch v0.1.2 --depth 1 https://github.com/tsopany/moodle-local_fullmcp.git /var/deploy/lms/fullmcp_plugin
   ```
   Update later: fetch tags and check out the new one in that folder, then redeploy `lms`. The image
   runs the Moodle upgrade itself at container start (`docker exec lms-lms-1 php
   /var/www/html/admin/cli/upgrade.php --non-interactive` by hand also works; "No upgrade needed" is
   normal when the start-up already did it).
2. `lms/docker-compose.yaml.template`, service `lms`:
   ```yaml
           environment:
               # A 100 MB backup is ~134 MB of base64 in one POST (image defaults: 50M, 50M, 50M, 256M)
               client_max_body_size: 150M
               post_max_size: 150M
               upload_max_filesize: 150M
               memory_limit: 512M
           volumes:
               - /var/deploy/lms/fullmcp_plugin:/var/www/html/public/local/fullmcp:ro
   ```
3. Same file, new service:
   ```yaml
       fullmcp:
           image: ghcr.io/tsopany/moodle-fullmcp-server:0.1.3
           restart: always
           environment:
               MOODLE_URL: https://lms.tsopany.com        # exact SITE_URL
               MOODLE_TOKEN: ${FULLMCP_MOODLE_TOKEN}
               MOODLE_ALLOW_WRITE: "true"
               FULLMCP_BEARER_TOKEN: ${FULLMCP_BEARER_TOKEN}
               FULLMCP_ALLOWED_HOSTS: lmsmcp.tsopany.com  # other Host headers get 421
           volumes:
               - /var/deploy/lms/fullmcp_files:/data/fullmcp-files
           depends_on:
               - lms
           networks:
               - lms_network
   ```
4. `lms/owners`: add `fullmcp_files 10001:10001` (the container runs as uid 10001).
5. `lms/secrets.sh`: add `\$FULLMCP_MOODLE_TOKEN \$FULLMCP_BEARER_TOKEN` to the `envsubst` whitelist
   (otherwise they render empty).
6. `caddy/Caddyfile`:
   ```
   # LMS MCP
   lmsmcp.tsopany.com {
       reverse_proxy lms-fullmcp-1:8000 {
           flush_interval -1
       }
   }
   ```
   `flush_interval -1` keeps streamed responses flowing. Caddy already joins `lms_lms_network`, so
   `caddy/docker-compose.yaml` needs no change.
7. DNS: A record for the MCP host to the VM (same IP as the Moodle host).
8. Infisical (project and env of that stack):
   - `FULLMCP_BEARER_TOKEN`: `openssl rand -hex 32`. Clients send it as `Authorization: Bearer ...`.
     The server refuses to start under 32 characters.
   - `FULLMCP_MOODLE_TOKEN`: put `todo` first; the real token comes from section 2.
9. Push to `main` (`Deploy lms` and `Deploy caddy` run), then check the plugin is installed. These
   must print the file list, the plugin version (`2026110805` for v0.1.2) and `local_fullmcp`:
   ```bash
   docker exec lms-lms-1 ls /var/www/html/public/local/fullmcp
   docker exec lms-lms-1 php -r 'define("CLI_SCRIPT",true);require "/var/www/html/config.php";echo get_config("local_fullmcp","version")."\n";'
   docker exec lms-lms-1 php -r 'define("CLI_SCRIPT",true);require "/var/www/html/config.php";echo $DB->get_field("external_services","shortname",["shortname"=>"local_fullmcp"])."\n";'
   ```

## 2. Moodle setup (once, in the browser, as admin)

1. `/admin/search.php?query=enablewebservices`: tick **Enable web services**, save.
2. `/admin/settings.php?section=webserviceprotocols`: enable **REST protocol** (open eye). Leave SOAP off.
3. `/user/editadvanced.php?id=-1`: create user `mcpbuilder` (plain user, not an admin).
4. `/admin/roles/define.php?action=add`: archetype **No role**, name "MCP builder", context **System**.
   Allow exactly:
   - `moodle/course:create`, `moodle/restore:restorecourse`, `moodle/backup:backupcourse`
   - `moodle/course:update`, `moodle/course:manageactivities`, `moodle/course:activityvisibility`,
     `moodle/course:sectionvisibility`
   - `mod/page:addinstance`, `mod/label:addinstance`, `mod/url:addinstance`, `mod/quiz:addinstance`,
     `mod/book:addinstance`, `mod/forum:addinstance`, `mod/choice:addinstance`, `mod/assign:addinstance`
   - `mod/book:edit`, `mod/quiz:manage`, `moodle/question:add`
   - `moodle/course:view`, `moodle/course:viewhiddenactivities`, `moodle/course:viewhiddensections`
   - The capability that lets the role open each activity type. Without these Moodle treats the
     activity as inaccessible and every call on it fails ("Course or activity not accessible"):
     `mod/page:view`, `mod/label:view`, `mod/url:view`, `mod/quiz:view`, `mod/assign:view`, and the
     three not named `:view`: `mod/book:read`, `mod/forum:viewdiscussion`, `mod/choice:choose`
   - `webservice/rest:use`

   Do not allow `moodle/site:config`, `moodle/backup:userinfo` (user data in backups),
   `moodle/course:delete`. Without `course:delete` the server cannot delete courses (by design).
5. `/admin/roles/assign.php?contextid=1`: assign "MCP builder" to `mcpbuilder`.
6. `/admin/settings.php?section=externalservices`: "Full MCP Service" > **Authorised users**: add
   `mcpbuilder`.
7. `/admin/webservice/tokens.php` > **Create token**: user `mcpbuilder`, service "Full MCP Service".
   Copy it.
8. Infisical: put it in `FULLMCP_MOODLE_TOKEN`, then `gh workflow run deploy-lms.yml -f reason="token"`.

## 3. Verify (in this order)

1. Liveness and auth (from anywhere). Expect `ok` and `401`:
   ```bash
   curl -s https://lmsmcp.tsopany.com/health
   curl -s -o /dev/null -w "%{http_code}\n" -X POST https://lmsmcp.tsopany.com/mcp
   ```
2. A real read through the whole chain. Copy `FULLMCP_BEARER_TOKEN` from Infisical to the clipboard,
   then, from a checkout of `moodle-fullmcp-server` with its venv (`pip install -e .`):
   ```bash
   FULLMCP_BEARER_TOKEN="$(pbpaste)" .venv/bin/python tests/http_check.py https://lmsmcp.tsopany.com/mcp
   ```
   Expect `tools: 37` and `list_courses error: False`.
3. Optional write test (creates a hidden course, a section, page, label, url and quiz, edits the
   page, deletes everything again; 13 checks):
   ```bash
   FULLMCP_BEARER_TOKEN="$(pbpaste)" .venv/bin/python tests/http_write_check.py https://lmsmcp.tsopany.com/mcp
   ```
   The empty hidden course "MCP write test ... delete me" stays (the role cannot delete courses).
   Delete it in Moodle: `/course/management.php`. Do not run this on PRD without a reason.

Client config (Claude Code style): URL `https://<mcp host>/mcp`, header
`Authorization: Bearer <FULLMCP_BEARER_TOKEN>`.

## Cloudflare

STG's Moodle host is behind Cloudflare (`server: cloudflare` in its response headers). Cloudflare
answers HTTP 403 to Python's default User-Agents (`Python-urllib`, and likely `python-httpx`), while
curl and browser User-Agents get 200 (seen on the VM). From server 0.1.3 the server sends
`User-Agent: fullmcp-server/<version>` and that works. If calls from the server still get 403 (or
on PRD with different rules), allow that User-Agent with a WAF skip rule, or turn off Browser
Integrity Check for the Moodle host.

Large restores: the server reaches Moodle through its public URL, so with Cloudflare proxying the
Moodle host, Cloudflare's request body limit (100 MB on Free and Pro) applies. A base64 restore is
about 1.34 times the .mbz, so restores above roughly 70 MB would fail at Cloudflare. Not tested. Fix:
the hairpin alias below sends the server's calls straight to Caddy, skipping Cloudflare. Also check
whether the MCP host itself is proxied by Cloudflare, where the same limit applies to the MCP request.

## Hairpin alias (use if the container cannot reach the Moodle host, or to bypass Cloudflare)

Give Caddy the name as a network alias in `caddy/docker-compose.yaml`:

```yaml
            - lms_external:
                  aliases:
                      - lms.tsopany.com
```

(the `networks:` list there becomes a map for that entry). Traffic then goes container -> Caddy.
On STG the container reached `https://lms.tsopany.com` without this (a timeout would indicate the
hairpin problem; HTTP 403 is Cloudflare, not hairpin).

## Troubleshooting (errors actually seen)

| Symptom | Cause | Fix |
|---|---|---|
| Server tools fail with HTTP 403 | Cloudflare blocks Python User-Agents | server 0.1.3+, or WAF rule |
| Moodle replies 303 / calls fail | `MOODLE_URL` is not the exact `SITE_URL` | set it to `SITE_URL` |
| `Course or activity not accessible` | role lacks the activity's view capability | add `mod/<type>:view` (or `book:read`, `forum:viewdiscussion`, `choice:choose`) |
| Most tools fail (core functions missing from the service) | plugin older than v0.1.1 | use v0.1.2 |
| HTTP 413 on restore | web server body limit | the four limits in section 1 step 2 |
| Image pull denied | package private | org setting "Package creation > Public", package visibility Public, or `docker login ghcr.io` |
| Plugin missing after deploy | image `PLUGINS=` installer used, or mount wrong | use the mount in section 1 |
| Server exits: "FULLMCP_BEARER_TOKEN must be set" | secret not rendered | whitelist in `secrets.sh`, redeploy |
| 421 from `/mcp` | `Host` header not in `FULLMCP_ALLOWED_HOSTS` | set it to the public MCP host |
| Container cannot write files | volume owner | `fullmcp_files 10001:10001` in `lms/owners` |
| `http_check.py` prompt hangs | `read -s` needs a real terminal | use the `pbpaste` form above |

## Operations

- Rotate the bearer token: new value in Infisical, `gh workflow run deploy-lms.yml`, update clients.
- Rotate the Moodle token: delete and recreate it at `/admin/webservice/tokens.php`, update
  Infisical, redeploy. Disable the `mcpbuilder` user to cut off the server at once.
- Update the plugin or server: bump the tag (section 1 step 1 for the plugin, the image tag in the
  compose file for the server), push, redeploy. Check section 3 afterwards.
- Writes can be turned off with `MOODLE_ALLOW_WRITE: "false"` (read tools stay on).

## Not done

- OAuth 2.1 for MCP (needed for claude.ai connectors and "any agent"); it needs an authorization
  server. Today one shared bearer token guards the endpoint: anyone holding it can create and delete
  course content as `mcpbuilder`. No rate limit, no per-client identity, no audit log. The part to
  replace is `_BearerAuth` in `fullmcp_server/server.py`.
- Tools that read or change people's data (users, grades, messages) are removed in v1 by decision.
- A real 100 MB backup/restore has not been tried on STG, and the Cloudflare limit above is untested.
- Tested on Moodle 5.3 only.
