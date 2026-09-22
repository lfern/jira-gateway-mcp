"""Prueba rápida de conectividad con Jira (y Bitbucket si está configurado),
sin pasar por el protocolo MCP.
Uso: python scripts/smoke_test.py
Lee las credenciales de ~/.jira-gateway.env automáticamente (o de
JIRA_GATEWAY_ENV_FILE si lo has cambiado) — no hace falta exportar nada.
"""
from gateway.bitbucket_client import BitbucketClient, BitbucketError
from gateway.config import Config, ConfigError
from gateway.jira_client import JiraClient, JiraError

try:
    cfg = Config.from_env()
except ConfigError as e:
    raise SystemExit(f"Config inválida: {e}")

client = JiraClient(cfg)

print(f"Consultando tareas de {cfg.jira_email} en proyecto {cfg.project_key}...")
try:
    tasks = client.list_my_tasks()
except JiraError as e:
    raise SystemExit(f"Error de Jira: {e}")

if not tasks:
    print("Sin tareas abiertas asignadas (o la query no devolvió nada).")
else:
    for t in tasks:
        print(f"  {t['key']}: {t['summary']} [{t['status']}]")

print()
if not cfg.bitbucket_configured:
    print("Bitbucket no configurado (BITBUCKET_EMAIL/API_TOKEN/WORKSPACE) — se omite.")
elif not cfg.bitbucket_default_repo:
    print("Bitbucket configurado pero sin BITBUCKET_DEFAULT_REPO — se omite el chequeo.")
else:
    bb = BitbucketClient(cfg)
    print(f"Consultando PRs abiertos de {cfg.bitbucket_workspace}/{cfg.bitbucket_default_repo}...")
    try:
        prs = bb.list_open_pull_requests(cfg.bitbucket_default_repo)
    except BitbucketError as e:
        raise SystemExit(f"Error de Bitbucket: {e}")

    if not prs:
        print("Sin PRs abiertos.")
    else:
        for pr in prs:
            print(f"  #{pr['id']}: {pr['title']} ({pr['source']} -> {pr['destination']})")
