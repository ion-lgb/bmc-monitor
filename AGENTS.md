# Repository Guidelines

## Project Structure & Module Organization

This repository is a Docker Compose stack for out-of-band BMC (IPMI) monitoring:

```text
bmc-manager/                 Flask web UI for adding/removing servers
ipmi_exporter/               ipmi_exporter config (ipmi.yml, gitignored)
prometheus/                  prometheus.yml + file_sd targets (JSON, gitignored)
grafana/provisioning/        Datasources and dashboards (JSON)
telegraf/                    Optional ESXi SNMP collection
docs/                        Screenshots used by the README
docker-compose.yml           Full stack definition
```

Runtime-generated files (`.env`, `ipmi_exporter/ipmi.yml`,
`prometheus/targets/bmc-targets.json`, `.snmp-creds`) are gitignored and
maintained by the web UI — never edit or commit them.

## Build, Test, and Development Commands

```bash
cp .env.example .env
cp ipmi_exporter/ipmi.yml.example ipmi_exporter/ipmi.yml
cp prometheus/targets/bmc-targets.json.example prometheus/targets/bmc-targets.json

docker compose up -d --build   # build and start the full stack
docker compose down            # stop the stack (data volumes persist)
docker compose up -d telegraf  # start the optional ESXi collector
```

For local bmc-manager development without Docker:

```bash
cd bmc-manager && pip install -r requirements.txt && python app.py
```

## Coding Style & Naming Conventions

- Python: PEP 8, 4-space indentation, type hints, dataclasses, and
  docstrings. Comments and user-facing text are Chinese; keep this pattern.
- Dashboard JSONs follow Grafana's provisioning schema; keep them readable
  and in sync with `docker-compose.yml` scrape intervals.
- Configs use 2-space YAML/JSON indentation.
- No formatter or linter is configured; match surrounding code manually.

## Testing Guidelines

No test suite exists yet. If you add one, place tests in
`bmc-manager/tests/`, use pytest, and name files `test_*.py`. Verify manually
with the run commands above, including the save-and-reload flow for server
add/remove.

## Commit & Pull Request Guidelines

Commit messages are concise Chinese summaries that state the change and its
motivation, e.g. `修正耗电量算法(实测高估 2.6 倍)` or `新增 ESXi 监控:Telegraf +
SNMP v3 -> 现有 Prometheus`.

Pull requests should include a description of what changed and why, reference
the issue number if applicable, add screenshots for UI or dashboard changes,
and confirm that no secrets or generated configs are included. If you touch
config handling, note that writes must remain atomic (temp file +
`os.replace`) and that configs are bind-mounted as directories, not files.

## Security Notes

- bmc-manager (8080) and Prometheus (9090) have no authentication; keep them
  on internal networks only.
- BMC passwords are stored in plaintext in `ipmi.yml`; never commit this file.
- Grafana exposes anonymous read-only access by design; root URL must point to
  the real host so shared links work.
