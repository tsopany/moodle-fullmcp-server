# Running fullmcp-server on the VM (in the `lms` stack)

For the `tsopany-vm-setup` repo. The server runs as a third service in
`lms/docker-compose.yaml.template`, on the same `lms_network` as Moodle, so Caddy (already attached
to `lms_lms_network`) can reach it with no change to `caddy/docker-compose.yaml`.

Prerequisite: the `local_fullmcp` plugin (>= v0.1.1, with the 14 core functions in its service) is
installed in the Moodle image and upgraded. Without it the server connects but its write tools fail.

## 1. Moodle (once, by an admin)

1. Site administration > Server > Web services: enable web services, enable the **REST** protocol.
2. Create a user for the server (not an admin) and a role "MCP builder" with the capabilities in
   the plugin's `docs/functions.md` ("Service-user role"). Assign it at system level.
3. Services: "Full MCP Service" > Authorised users > add that user. Then create a token for that
   user and that service. This is `FULLMCP_MOODLE_TOKEN`.

## 2. Infisical (project `lms`, env `stg`)

- `FULLMCP_MOODLE_TOKEN`: the token from step 1.
- `FULLMCP_BEARER_TOKEN`: what clients send as `Authorization: Bearer ...`. Make it with
  `openssl rand -hex 32`. (A stopgap until OAuth; the server refuses to start if it is under 32 chars.)

Add both names to the `envsubst` whitelist in `lms/secrets.sh`:
`'\$DB_USER \$DB_PASSWORD \$DB_DATABASE \$LMS_ADMIN_PASSWORD \$FULLMCP_MOODLE_TOKEN \$FULLMCP_BEARER_TOKEN'`

## 3. `lms/docker-compose.yaml.template`: new service

```yaml
    fullmcp:
        image: ghcr.io/tsopany/fullmcp-server:0.1.0
        restart: always
        environment:
            # Must be the exact SITE_URL of Moodle. Moodle answers any other host name with a
            # 303 redirect to SITE_URL (seen in testing), so do not use http://lms:8080 here.
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

- Add `fullmcp_files 10001:10001` to `lms/owners` (the container runs as uid 10001).
- The image is on ghcr.io; make the package public, or `docker login ghcr.io` on the VM.
- `/health` (no auth) is used by the image's own healthcheck.

## 4. Caddyfile

```
# LMS MCP
lmsmcp.tsopany.com {
    reverse_proxy lms-fullmcp-1:8000 {
        flush_interval -1
    }
}
```

`flush_interval -1` keeps streamed responses flowing. Add a DNS A record for `lmsmcp.tsopany.com`
pointing at the VM.

## 5. Check

```bash
curl -s https://lmsmcp.tsopany.com/health                 # ok
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://lmsmcp.tsopany.com/mcp   # 401
```

Client config (Claude Code style): URL `https://lmsmcp.tsopany.com/mcp`, header
`Authorization: Bearer <FULLMCP_BEARER_TOKEN>`.

## If the server cannot reach https://lms.tsopany.com from inside the VM

That is a "hairpin" problem (the VM cannot connect to its own public address from a container).
Fix: give Caddy the name as a network alias in `caddy/docker-compose.yaml`:

```yaml
            - lms_external:
                  aliases:
                      - lms.tsopany.com
```

(the `networks:` list there becomes a map for that entry). Traffic then goes container -> Caddy.

## Not done yet

- OAuth 2.1 for MCP (needed for claude.ai connectors and "any agent"): needs an authorization
  server. The bearer check in `fullmcp_server/server.py` (`_BearerAuth`) is the part to replace.
- Large uploads through Moodle's own nginx: see the plugin README ("Backup and restore size").
