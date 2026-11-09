# Running fullmcp-server on the VM (in the `lms` stack)

For the `tsopany-vm-setup` repo. The server runs as a service in `lms/docker-compose.yaml.template`,
on the same `lms_network` as Moodle. Caddy (already attached to `lms_lms_network`) reaches it with no
change to `caddy/docker-compose.yaml`.

Versions used: plugin `local_fullmcp` **v0.1.2**, image
`ghcr.io/tsopany/moodle-fullmcp-server:0.1.3` (public). Plugin v0.1.0 lacks the 14 core functions in
its service, so most tools would fail. Server versions before 0.1.3 get HTTP 403 from Cloudflare (see
"Cloudflare" below).

## How deploys work

`tsopany-vm-setup` deploys by GitHub Actions. A push to `main` that touches `lms/**` or `caddy/**`
runs `Deploy lms` / `Deploy caddy`: upload the files, run `secrets.sh` (renders the template from
Infisical), `docker compose up -d --force-recreate`. To redeploy without a file change (for example
after changing a secret in Infisical):

```bash
cd tsopany-vm-setup && gh workflow run deploy-lms.yml -f reason="why"
```

## 1. One-time VM and repo changes

1. Clone the plugin on the VM. The Moodle image's own `PLUGINS=` installer does not work in 5.3 (it
   copies to `/var/www/html/local/`, but 5.3 serves from `public/`), so the plugin is mounted:
   ```bash
   sudo git clone --branch v0.1.2 --depth 1 https://github.com/tsopany/moodle-local_fullmcp.git /var/deploy/lms/fullmcp_plugin
   ```
   To update later: fetch tags and check out the new one in that folder, then redeploy `lms`. The
   image runs the Moodle upgrade itself at start (`admin/cli/upgrade.php --non-interactive` by hand
   also works).
2. `lms/docker-compose.yaml.template`, service `lms`:
   ```yaml
           environment:
               # 100 MB backups are ~134 MB of base64 in one POST (defaults: 50M, 50M, 50M, 256M)
               client_max_body_size: 150M
               post_max_size: 150M
               upload_max_filesize: 150M
               memory_limit: 512M
           volumes:
               - /var/deploy/lms/fullmcp_plugin:/var/www/html/public/local/fullmcp:ro
   ```
3. New service in the same file:
   ```yaml
       fullmcp:
           image: ghcr.io/tsopany/moodle-fullmcp-server:0.1.3
           restart: always
           environment:
               # Must be the exact SITE_URL of Moodle. Moodle answers any other host name with a
               # 303 redirect to SITE_URL, so do not use http://lms:8080 here.
               MOODLE_URL: https://lms.tsopany.com
               MOODLE_TOKEN: ${FULLMCP_MOODLE_TOKEN}
               MOODLE_ALLOW_WRITE: "true"
               FULLMCP_BEARER_TOKEN: ${FULLMCP_BEARER_TOKEN}
               FULLMCP_ALLOWED_HOSTS: lmsmcp.tsopany.com
           volumes:
               - /var/deploy/lms/fullmcp_files:/data/fullmcp-files
           depends_on:
               - lms
           networks:
               - lms_network
   ```
4. `lms/owners`: add `fullmcp_files 10001:10001` (the container runs as uid 10001).
5. `lms/secrets.sh`: add `\$FULLMCP_MOODLE_TOKEN \$FULLMCP_BEARER_TOKEN` to the `envsubst` whitelist.
6. `caddy/Caddyfile`:
   ```
   # LMS MCP
   lmsmcp.tsopany.com {
       reverse_proxy lms-fullmcp-1:8000 {
           flush_interval -1
       }
   }
   ```
   `flush_interval -1` keeps streamed responses flowing.
7. DNS: A record `lmsmcp.tsopany.com` to the VM (same IP as `lms.tsopany.com`).
8. Infisical (project `lms`, env `stg`):
   - `FULLMCP_BEARER_TOKEN`: `openssl rand -hex 32` (what clients send as `Authorization: Bearer ...`;
     a stopgap until OAuth; the server refuses to start under 32 chars).
   - `FULLMCP_MOODLE_TOKEN`: put `todo` for now, the real token comes from section 2.

Push to `main`; `Deploy lms` and `Deploy caddy` run.

Check the plugin is installed (all three must succeed):

```bash
docker exec lms-lms-1 ls /var/www/html/public/local/fullmcp
docker exec lms-lms-1 php -r 'define("CLI_SCRIPT",true);require "/var/www/html/config.php";echo get_config("local_fullmcp","version")."\n";'
docker exec lms-lms-1 php -r 'define("CLI_SCRIPT",true);require "/var/www/html/config.php";echo $DB->get_field("external_services","shortname",["shortname"=>"local_fullmcp"])."\n";'
```

The second prints the plugin version (`2026110805`), the third prints `local_fullmcp`.

## 2. Moodle setup (once, in the browser, as admin)

1. `/admin/search.php?query=enablewebservices`: tick **Enable web services**, save.
2. `/admin/settings.php?section=webserviceprotocols`: enable **REST protocol** (open eye). Leave SOAP off.
3. `/user/editadvanced.php?id=-1`: create user `mcpbuilder` (not an admin).
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
     three that are not named `:view`: `mod/book:read`, `mod/forum:viewdiscussion`, `mod/choice:choose`
   - `webservice/rest:use`

   Do not allow `moodle/site:config`, `moodle/backup:userinfo`, `moodle/course:delete`.
5. `/admin/roles/assign.php?contextid=1`: assign "MCP builder" to `mcpbuilder`.
6. `/admin/settings.php?section=externalservices`: "Full MCP Service" > **Authorised users**: add
   `mcpbuilder`.
7. `/admin/webservice/tokens.php` > **Create token**: user `mcpbuilder`, service "Full MCP Service".
   Copy it.
8. Infisical: put it in `FULLMCP_MOODLE_TOKEN`. Run `gh workflow run deploy-lms.yml -f reason="token"`.

## 3. Checks

```bash
curl -s https://lmsmcp.tsopany.com/health                                          # ok
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://lmsmcp.tsopany.com/mcp    # 401
docker exec lms-fullmcp-1 python -c "import urllib.request as u;r=u.Request('https://lms.tsopany.com/login/index.php',headers={'User-Agent':'fullmcp-server'});print(u.urlopen(r).status)"
```

The last one checks that the container can reach Moodle at its public address. Expect `200`. A
timeout would mean a hairpin problem (the VM cannot connect to its own public address from a
container); fix below. It sends its own User-Agent on purpose, see "Cloudflare".

Client config (Claude Code style): URL `https://lmsmcp.tsopany.com/mcp`, header
`Authorization: Bearer <FULLMCP_BEARER_TOKEN>`.

## Cloudflare

`lms.tsopany.com` is behind Cloudflare. It answers HTTP 403 to Python's default User-Agents
(`Python-urllib`, and likely `python-httpx`), while curl and browser User-Agents get 200 (seen on the
VM). From 0.1.3 the server sends `User-Agent: fullmcp-server/<version>`. If Moodle calls from the
server still get 403, allow that User-Agent in Cloudflare (a WAF skip rule) or turn off Browser
Integrity Check for `lms.tsopany.com`.

## Hairpin fix, if the container cannot reach https://lms.tsopany.com

Give Caddy the name as a network alias in `caddy/docker-compose.yaml`:

```yaml
            - lms_external:
                  aliases:
                      - lms.tsopany.com
```

(the `networks:` list there becomes a map for that entry). Traffic then goes container -> Caddy.

## Not done yet

- OAuth 2.1 for MCP (needed for claude.ai connectors and "any agent"): needs an authorization
  server. The bearer check in `fullmcp_server/server.py` (`_BearerAuth`) is the part to replace.
- Large uploads: see the plugin README ("Backup and restore size"). The limits above are set, but a
  real 100 MB restore has not been tried.
