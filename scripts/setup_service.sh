#!/usr/bin/env bash
# Despliega jira-gateway como servicio systemd bajo un usuario Unix separado
# y exclusivo de una empresa/workspace, para que el token de Jira de esa
# empresa quede fuera del alcance de quien lance Claude Code -- ni por
# `cat`, ni por nada que corra como tu usuario normal puede leer
# /opt/jira-gateway-<empresa> una vez desplegado.
#
# Soporta varias empresas en la misma máquina, cada una con su propio
# usuario/carpeta/unidad systemd, pero TODAS escuchan en el mismo puerto fijo
# (MCP_PORT, por defecto 8765) -- nunca hay dos activas a la vez (alterna con
# scripts/jira-switch.sh), así la config MCP de Claude Code no cambia nunca
# entre empresas y no hay dos servicios compitiendo por el mismo puerto.
#
# Uso: sudo bash scripts/setup_service.sh <empresa>
#   <empresa>: slug corto, solo [a-z0-9-], ej. "acme" o "cliente-b"
#
# Idempotente. Crea el usuario y /opt/jira-gateway-<empresa>, y si el .env
# de ahí dentro no existe todavía, crea uno con las variables vacías (600,
# propiedad del usuario de servicio) -- edítalo tú después con las
# credenciales reales (sudo -u jira-gw-<empresa> nano /opt/.../.env, o
# sudoedit). Si ya existe, no lo toca. En la misma pasada publica el
# código, instala dependencias y crea/actualiza la unidad systemd (no la
# arranca -- usa jira-switch.sh para eso).

set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  echo "Este script necesita sudo (crea un usuario de sistema y una unidad systemd)." >&2
  echo "Uso: sudo bash scripts/setup_service.sh <empresa>" >&2
  exit 1
fi

SLUG="${1:-}"
if [[ -z "$SLUG" ]]; then
  echo "Falta el nombre de empresa." >&2
  echo "Uso: sudo bash scripts/setup_service.sh <empresa>" >&2
  shopt -s nullglob
  units=(/etc/systemd/system/jira-gateway-*.service)
  if [[ ${#units[@]} -gt 0 ]]; then
    echo "Instancias ya desplegadas:" >&2
    for unit in "${units[@]}"; do
      echo "  - $(basename "$unit" .service | sed 's/^jira-gateway-//')" >&2
    done
  fi
  exit 1
fi
if ! [[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{0,19}$ ]]; then
  echo "Nombre de empresa inválido: '$SLUG'. Solo minúsculas, dígitos y '-', máx 20 caracteres." >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER="jira-gw-$SLUG"
SVC_HOME="/opt/jira-gateway-$SLUG"
UNIT_NAME="jira-gateway-$SLUG.service"
SVC_ENV="$SVC_HOME/.env"
PORT="${MCP_PORT:-8765}"

echo "==> 1. Usuario y directorio de servicio ($SVC_USER, $SVC_HOME)"
if ! id -u "$SVC_USER" &>/dev/null; then
  useradd --system --create-home --home-dir "$SVC_HOME" --shell /usr/sbin/nologin "$SVC_USER"
  echo "    creado: $SVC_USER"
else
  echo "    ya existe: $SVC_USER"
fi

echo "==> 2. Credenciales ($SVC_ENV)"
CREDS_PENDING=0
if [[ -f "$SVC_ENV" ]]; then
  echo "    ya existen, no las toco."
else
  cat > "$SVC_ENV" <<'EOF'
# Rellena estos valores (mismo formato que la sección Configuración del
# README). El servicio no arrancará hasta que estén las obligatorias de Jira.
JIRA_EMAIL=
JIRA_API_TOKEN=
JIRA_CLOUD_ID=
JIRA_SITE_URL=
JIRA_PROJECT_KEY=
JIRA_IN_PROGRESS_STATUS=In Progress
JIRA_SELECTED_STATUS=Selected for Development
JIRA_DEFAULT_ISSUE_TYPE=Task
JIRA_SUBTASK_ISSUE_TYPE=Subtask

# Bitbucket es opcional -- si se deja vacío, el servicio arranca igual solo
# con Jira. Ver la sección Bitbucket del README para el token y los scopes.
BITBUCKET_EMAIL=
BITBUCKET_API_TOKEN=
BITBUCKET_WORKSPACE=
BITBUCKET_ALLOWED_REPOS=
BITBUCKET_DEFAULT_REPO=
BITBUCKET_DEFAULT_TARGET_BRANCH=pre
EOF
  chown "$SVC_USER":"$SVC_USER" "$SVC_ENV"
  chmod 600 "$SVC_ENV"
  CREDS_PENDING=1
  echo "    creado vacío en $SVC_ENV (600, propiedad de $SVC_USER) -- edítalo con las credenciales reales."
fi

echo "==> 3. Publicando código en $SVC_HOME (sin .venv, .env, .git, __pycache__)"
rsync -a --delete \
  --exclude='.venv' --exclude='.env' --exclude='.git' \
  --exclude='__pycache__' --exclude='*.egg-info' --exclude='scripts' \
  "$SRC_DIR"/ "$SVC_HOME"/
chown -R "$SVC_USER":"$SVC_USER" "$SVC_HOME"
chmod 700 "$SVC_HOME"

echo "==> Instalando Python 3.11 + dependencias como $SVC_USER (aislado)"
# PATH fijo y mínimo (no depender del PATH de quien invoca sudo) y
# --reinstall en el Python gestionado: un intento anterior fallido puede
# haber dejado en caché ($HOME/.local/share/uv/python) un CPython 3.11
# incompleto/corrupto para este usuario -- uv lo reutiliza sin volver a
# descargar y falla con un error confuso ("Python installation is missing
# a `_sysconfigdata_` file") en vez de detectar que está roto. --reinstall
# fuerza una descarga limpia en cada redeploy.
sudo -u "$SVC_USER" -H bash -c '
  set -e
  export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  if [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  cd '"$SVC_HOME"'
  "$HOME/.local/bin/uv" python install 3.11 --reinstall
  "$HOME/.local/bin/uv" venv --python 3.11 --managed-python --clear
  "$HOME/.local/bin/uv" pip install --python .venv/bin/python -e .
'

if ! grep -q '^MCP_TRANSPORT=' "$SVC_ENV"; then
  {
    echo ""
    echo "MCP_TRANSPORT=streamable-http"
    echo "MCP_HOST=127.0.0.1"
    echo "MCP_PORT=$PORT"
  } >> "$SVC_ENV"
fi

echo "==> Unidad systemd"
cat > "/etc/systemd/system/$UNIT_NAME" <<UNIT
[Unit]
Description=Jira Gateway MCP server -- $SLUG (usuario aislado $SVC_USER)
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

echo ""
if [[ "$CREDS_PENDING" -eq 1 ]]; then
  echo "==> Antes de activarlo, edita las credenciales reales:"
  echo "        sudo -u $SVC_USER nano $SVC_ENV"
  echo ""
fi
echo "==> Desplegado, pero NO arrancado a propósito -- todas las empresas"
echo "    comparten el puerto $PORT y solo una puede escuchar a la vez."
echo "    Actívalo con:"
echo "        sudo bash scripts/jira-switch.sh $SLUG"
echo ""
echo "    La config MCP de Claude Code no depende de qué empresa esté"
echo "    activa, usa siempre la misma URL:"
echo '        { "type": "http", "url": "http://127.0.0.1:'"$PORT"'/mcp" }'
