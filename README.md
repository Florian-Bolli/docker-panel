# Docker Panel

A small web page that lists the Docker containers on this machine and lets you start, stop and delete them, and watch their logs.

It is a single Python file (`app.py`) that uses only the standard library. It talks to Docker directly through `/var/run/docker.sock`.

## Open it

| Address | Needs |
|---|---|
| http://docker.local | `local-proxy` running + `/etc/hosts` entry |
| http://containers.localhost | `local-proxy` running |
| http://localhost:9001 | only the panel itself (fallback) |

Type the `http://` explicitly, otherwise the browser may try HTTPS (`SSL_ERROR_RX_RECORD_TOO_LONG`) or run a search.

## Features

- All containers, running ones first, grouped by compose project
- Start / Stop with a loading state ("Starting…", "Stopping…", and "Starting… (health check)" until a health check passes)
- Clickable links to each container's published ports
- **Details ▾** dropdown per container:
  - Live logs: the last 200 lines, then new lines every 1.5 s
  - Delete container, with a confirmation. Running containers are stopped first. Volumes and images are kept.
- Extra warning before stopping or deleting `local-proxy`, since the panel's nice addresses depend on it
- Refreshes every 5 s (every 1 s while something is starting or stopping)

## How it runs

```
browser ──> local-proxy (Caddy, port 80) ──> app.py (127.0.0.1:9001) ──> /var/run/docker.sock
```

### The panel: systemd user service

Unit file: `~/.config/systemd/user/docker-panel.service`. It starts at boot; lingering is enabled for this user.

```bash
systemctl --user status docker-panel
systemctl --user restart docker-panel     # after editing app.py
journalctl --user -u docker-panel -f      # server logs
```

### The proxy: `local-proxy` container

Caddy in a container on the host network. It listens on port 80, only on `127.0.0.1` and `::1`. It uses Docker instead of a system install, so no sudo is needed. Config: `Caddyfile` in this folder.

```bash
docker restart local-proxy                # after editing Caddyfile
```

To recreate it (for example after deleting it):

```bash
cd ~/Programs/docker-panel
docker run -d --name local-proxy --restart=always --network host \
  -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2
```

### `docker.local`

`.localhost` names resolve to this machine automatically. `.local` needs a hosts entry:

```bash
echo '127.0.0.1 docker.local' | sudo tee -a /etc/hosts
```

`.local` is also used by mDNS. If a device on the network ever calls itself `docker`, use `containers.localhost` instead.

## Adding a new hostname

1. Add the name to the site line in `Caddyfile`, or add a new block for another service:
   ```
   http://grafana.localhost {
   	bind 127.0.0.1 [::1]
   	reverse_proxy 127.0.0.1:3005
   }
   ```
2. `docker restart local-proxy`
3. **For the panel only:** add the hostname to `ALLOWED_HOSTS` in `app.py`, then `systemctl --user restart docker-panel`. Otherwise the panel answers `403 bad host`.

## Security

Anyone with access to the panel can control Docker, which is effectively root on this machine. The panel is locked down:

- It listens on `127.0.0.1` only, and the proxy binds to loopback only, so it is not reachable from the network.
- Every request's `Host` header must be in `ALLOWED_HOSTS`. This blocks DNS-rebinding attacks.
- Every API call must carry the header `X-Panel: 1`. Other websites open in the browser can't send it without a CORS preflight, which the panel never approves. This blocks cross-site requests that would start, stop or delete containers.
- There is no login. Don't expose it beyond localhost without adding authentication.

## API

All endpoints require `X-Panel: 1`.

| Method | Path | Does |
|---|---|---|
| GET | `/api/containers` | List all containers (id, name, image, state, status, health, ports, project) |
| GET | `/api/logs/<id>[?since=<sec.nanos>]` | Last 200 log lines, or only lines newer than the cursor; returns `{lines, cursor}` |
| POST | `/api/start/<id>` | Start |
| POST | `/api/stop/<id>` | Stop |
| POST | `/api/remove/<id>` | Delete (force; volumes kept) |

```bash
curl -H 'X-Panel: 1' http://localhost:9001/api/containers
```

## Limitations

- "Running" is not the same as "ready". Readiness is only shown for containers that define a health check.
- Logs come from polling, not streaming, so they can lag by up to 1.5 s. ANSI colors are stripped.
- No restart button, no image or volume management. Use the Docker CLI for those.
