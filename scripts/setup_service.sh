#!/usr/bin/env bash
# Despliega jira-gateway como servicio systemd bajo un usuario Unix separado
# (jira-gw), para que el token de Jira quede fuera del alcance de quien
# lance Claude Code (lfern) -- ni por `cat`, ni por `docker exec`, nada que
# corra como lfern puede leer /opt/jira-gateway una vez desplegado.
#
# Uso: sudo bash scripts/setup_service.sh
#
# Idempotente: se puede volver a ejecutar tras cambiar el código en
# gateway/ para redesplegar. Si /opt/jira-gateway/.env ya existe, NO lo
# vuelve a copiar desde el repo de desarrollo (así puedes borrar el .env
# del repo dev sin que un redeploy futuro se quede sin credenciales).

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Este script necesita sudo (crea un usuario de sistema y una unidad systemd)." >&2
  echo "Uso: sudo bash scripts/setup_service.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER="jira-gw"
SVC_HOME="/opt/jira-gateway"
DEV_ENV="$SRC_DIR/.env"
SVC_ENV="$SVC_HOME/.env"
PORT="${MCP_PORT:-8765}"

echo "==> Usuario de servicio"
if ! id -u "$SVC_USER" &>/dev/null; then
  useradd --system --create-home --home-dir "$SVC_HOME" --shell /usr/sbin/nologin "$SVC_USER"
  echo "    creado: $SVC_USER (home $SVC_HOME, sin login)"
else
  echo "    ya existe: $SVC_USER"
fi

echo "==> Desplegando código a $SVC_HOME (sin .venv, .env, .git, __pycache__)"
rsync -a --delete \
  --exclude='.venv' --exclude='.env' --exclude='.git' \
  --exclude='__pycache__' --exclude='*.egg-info' --exclude='scripts' \
  "$SRC_DIR"/ "$SVC_HOME"/
chown -R "$SVC_USER":"$SVC_USER" "$SVC_HOME"
chmod 700 "$SVC_HOME"

echo "==> Instalando Python 3.11 + dependencias como $SVC_USER (aislado, no usa el uv de lfern)"
sudo -u "$SVC_USER" -H bash -c '
  set -e
  if [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  cd '"$SVC_HOME"'
  "$HOME/.local/bin/uv" venv --python 3.11
  "$HOME/.local/bin/uv" pip install --python .venv/bin/python -e .
'

echo "==> Credenciales"
if [[ -f "$SVC_ENV" ]]; then
  echo "    $SVC_ENV ya existe, no lo toco (edítalo a mano como $SVC_USER si hace falta cambiar algo)."
elif [[ -f "$DEV_ENV" ]]; then
  cp "$DEV_ENV" "$SVC_ENV"
  {
    echo ""
    echo "MCP_TRANSPORT=streamable-http"
    echo "MCP_HOST=127.0.0.1"
    echo "MCP_PORT=$PORT"
  } >> "$SVC_ENV"
  chown "$SVC_USER":"$SVC_USER" "$SVC_ENV"
  chmod 600 "$SVC_ENV"
  echo "    copiado desde $DEV_ENV a $SVC_ENV (600, propiedad de $SVC_USER)."
  echo "    Puedes borrar ahora $DEV_ENV -- ya no hace falta para el servicio."
else
  echo "    ERROR: no encuentro $DEV_ENV ni $SVC_ENV. Crea $SVC_ENV a mano como $SVC_USER (chmod 600) antes de arrancar el servicio." >&2
  exit 1
fi

echo "==> Unidad systemd"
cat > /etc/systemd/system/jira-gateway.service <<UNIT
[Unit]
Description=Jira Gateway MCP server (usuario aislado $SVC_USER)
After=network.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$SVC_HOME
EnvironmentFile=$SVC_ENV
ExecStart=$SVC_HOME/.venv/bin/python -m gateway.server
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$SVC_HOME

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now jira-gateway.service

sleep 1
echo "==> Estado del servicio"
systemctl --no-pager status jira-gateway.service | head -8

echo ""
echo "==> Listo. En la config MCP de Claude Code usa (SIN credenciales, ya viven en $SVC_USER):"
echo '    { "type": "http", "url": "http://127.0.0.1:'"$PORT"'/mcp" }'
