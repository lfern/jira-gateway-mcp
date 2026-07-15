"""Prueba rápida de conectividad con Jira, sin pasar por el protocolo MCP.
Uso: python scripts/smoke_test.py
Requiere las variables de entorno de siempre ya exportadas.
"""
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
