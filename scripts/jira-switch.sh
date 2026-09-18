#!/usr/bin/env bash
# Activa el jira-gateway de una empresa concreta y para cualquier otra
# instancia jira-gateway-*.service que estuviera activa, para que nunca haya
# más de un workspace/empresa escuchando a la vez en el puerto fijo -- así
# ningún proceso local (incluido tu propio agente) puede alcanzar por red
# las credenciales de una empresa con la que no estás trabajando ahora
# mismo, sencillamente porque ese servicio no está corriendo.
#
# Uso:
#   sudo bash scripts/jira-switch.sh <empresa>   # activa <empresa>, para el resto
#   bash scripts/jira-switch.sh                  # solo lista el estado (no necesita sudo)

set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

list_status() {
  echo "Instancias jira-gateway desplegadas:"
  shopt -s nullglob
  local units=(/etc/systemd/system/jira-gateway-*.service)
  if [[ ${#units[@]} -eq 0 ]]; then
    echo "  (ninguna -- despliega con: sudo bash scripts/setup_service.sh <empresa>)"
    return
  fi
  for unit in "${units[@]}"; do
    local name slug state
    name="$(basename "$unit" .service)"
    slug="${name#jira-gateway-}"
    state="$(systemctl is-active "$name" 2>/dev/null || true)"
    printf "  %-20s %s\n" "$slug" "${state:-inactive}"
  done
}

if [[ $# -eq 0 ]]; then
  list_status
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  echo "Cambiar de instancia activa necesita sudo." >&2
  echo "Uso: sudo bash scripts/jira-switch.sh $1" >&2
  exit 1
fi

SLUG="$1"
TARGET="jira-gateway-${SLUG}.service"

if [[ ! -f "/etc/systemd/system/$TARGET" ]]; then
  echo "No existe $TARGET." >&2
  echo "Despliega antes con: sudo bash scripts/setup_service.sh $SLUG" >&2
  exit 1
fi

echo "==> Parando cualquier otra instancia jira-gateway-* activa"
shopt -s nullglob
for unit in /etc/systemd/system/jira-gateway-*.service; do
  name="$(basename "$unit")"
  if [[ "$name" != "$TARGET" ]] && systemctl is-active --quiet "$name"; then
    echo "    parando $name"
    systemctl disable --now "$name" >/dev/null 2>&1 || true
  fi
done

echo "==> Activando $TARGET"
systemctl enable --now "$TARGET"
sleep 1
systemctl --no-pager status "$TARGET" | head -8

echo ""
echo "Activo ahora: $SLUG"
