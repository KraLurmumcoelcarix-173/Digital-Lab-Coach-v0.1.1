# Release & Course-Deployment Guide

The operational runbook for shipping DLC to a class. Written for the
instructor teaching undergraduates with Digital; students never need to read this file.
Assumes you have been working on a fork of DLC in order to fit it to your classroom.
Version numbers below are examples — use your own tag.

## What you have

Three pieces:

1. **A release zip** 
   Students: download → unzip → double-click `START_HERE` → point 
   at `Digital.jar` → paste the course URL + token in Settings →
   debug circuit. Windows uses `START_HERE.bat`, macOS/Linux uses `./start.sh`.

2. **The course proxy** 
   a small server holding YOUR API key, with per-machine daily limits, 
   a whole-server daily circuit breaker, telemetry ingest, and the admin dashboard.

3. **Two secrets**: the course token students paste once, and the
   admin token only you hold that opens the admin dashboard.

Prerequisites: all tests in your fork are green (`uv run pytest -q` —
with `DIGITAL_JAR` set so the jar-gated tests run too).

## 1. Build the zip and cut the GitHub release

1. Bump `version` in `pyproject.toml` and the Status line at the top of
   the README, commit.

2. Build the student zip:
   ```bash
   uv run python scripts/make_release_zip.py
   ```

3. Tag and push:
   ```bash
   git tag v0.1.2
   git push origin v0.1.2
   ```
4. On GitHub: **Releases → Draft a new release** → choose tag,
   title, and attach `dist/DigitalLabCoach.zip` as a release asset. 
   Keep the filename exactly `DigitalLabCoach.zip` — the README Download
   button URL depends on it and will keep working for every future version. 
   Publish.

5. Click the README's **Download** button to confirm it serves your zip.

## 2. Generate the course secrets

Run once per any reasonable range of time:

```bash
python -c "import secrets; print('course-' + secrets.token_urlsafe(18))"
python -c "import secrets; print('admin-'  + secrets.token_urlsafe(18))"
```

The two lines printed are the **course token** (goes to students) and the
**admin token**. Save both in a private note **outside any git folder**.

## 3. Run the course proxy

Use the built-in proxy if and only if you are collecting student data for 
classroom improvement study and you have had IRB permission from your 
department, else adjust the code in proxy/ to fit in the classroom.

The proxy holds your API key, enforces per-machine daily limits that
survive re-downloads, collects the anonymized telemetry, and serves the
admin dashboard. Full endpoint reference: [../proxy/README.md](../proxy/README.md).

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # your course key, not retrievable through any endpoint
export DLC_COURSE_TOKEN=<course token from step 2>
export DLC_ADMIN_TOKEN=<admin token from step 2>
export DLC_PROXY_DB=/path/to/dlc_proxy.db     # keep this file safe
uv run uvicorn proxy.dlc_proxy:app --host 0.0.0.0 --port 8321
```

Sanity check: `curl http://localhost:8321/v1/health` — expect
`"key_configured":true`, `"key_format_ok":true`,
`"course_token_set":true`. Startup also prints a warning if the key looks
malformed or the course token is missing.

### Spend protection (three layers by default, feel free to modify)

| Layer | Default | Tune with |
|---|---|---|
| Per-student daily caps | Mode A 1/day, Mode B 2/day | `dlc/l3/limits.py` `CAPS`; enforced only when the student app runs with `DLC_ENFORCE_LIMITS=1` (the release launchers set it) |
| Per-machine proxy backstop | modeA 8, modeB 10, grade 2, explain 2 calls/day | `CALL_BUDGETS` in `proxy/dlc_proxy.py` |
| Whole-server circuit breaker | 600 calls/day AND $20 est./day | env `DLC_GLOBAL_DAILY_CALLS`, `DLC_GLOBAL_DAILY_USD` |

The README's *Where to change what* table lists every other knob
(models, timeout, manifests, official tests, ROM payload, solutions
folder).

If the breaker is triggered, every AI request answers "the course server has
reached its daily capacity" until midnight (server time).

### Option A — your own machine

Works exactly like the test setup: students on the same network use
`http://<your-LAN-IP>:8321`. Machine must stay on while students work.

### Option B — small cloud VM (recommended)

Any ~$5/month VPS (1 vCPU / 1 GB) is plenty.

1. Clone the repo on the VM, install uv, `uv sync`.
2. Put the env vars in a systemd unit so the proxy survives reboots:
   ```ini
   # /etc/systemd/system/dlc-proxy.service
   [Unit]
   Description=DLC course proxy
   After=network.target
   [Service]
   WorkingDirectory=/opt/dlc
   Environment=ANTHROPIC_API_KEY=sk-ant-...
   Environment=DLC_COURSE_TOKEN=...
   Environment=DLC_ADMIN_TOKEN=...
   Environment=DLC_PROXY_DB=/opt/dlc/dlc_proxy.db
   ExecStart=/root/.local/bin/uv run uvicorn proxy.dlc_proxy:app --host 127.0.0.1 --port 8321
   Restart=on-failure
   [Install]
   WantedBy=multi-user.target
   ```
   `systemctl enable --now dlc-proxy`
3. **HTTPS** (so tokens are never sent in the clear): put
   [Caddy](https://caddyserver.com) in front — a 2-line Caddyfile gets an
   automatic Let's Encrypt certificate:
   ```
   dlc.your-domain.edu {
       reverse_proxy 127.0.0.1:8321
   }
   ```
   Students then use `https://dlc.your-domain.edu` as the course URL.

## 4. Watch admin dashboard (optional)

Open **`http://<proxy-host>:8321/admin/view`** (or your HTTPS URL +
`/admin/view`) in a browser. Enter the **admin token** once:

- machines with install_id hashed, events, today's LLM calls vs cap, today's and
  all-time estimated spend, breaker state, key health;
- the machines table (first/last seen, per-machine event and call counts);
- per-day activity and per-day LLM usage by machine and feature.

Raw data examples: `/admin/summary`, `/admin/daily`

## 5. Announce to students

You got this lol.

## 6. Rotating the course token

Any time necessary:

1. Generate a new course token (step 2 command).
2. Restart the proxy with the new `DLC_COURSE_TOKEN`.
3. Announce the new token; students paste it in Settings → Course server.

No re-release, no re-download; identities, history, and limits are
untouched.

## 7. End-of-release checklist

- [ ] Suite green (`uv run pytest -q`, with `DIGITAL_JAR` set) on the tagged commit.
- [ ] Release published; download + `START_HERE` tested on a clean machine.
- [ ] Proxy up with fresh tokens; `/v1/health` all-true; dashboard loads.
- [ ] One end-to-end student flow through the proxy (keyless machine).
- [ ] **Rotate the development API key** and set the new one only in the
      proxy env
