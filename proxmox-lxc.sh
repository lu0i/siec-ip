#!/usr/bin/env bash
# Instaluje skaner sieci w nowym kontenerze LXC (Debian) na Proxmoxie.
# Uruchom w powłoce Proxmoxa jako root (panel WWW: węzeł → Shell, albo SSH):
#
#     bash /root/siec-ip/proxmox-lxc.sh
#
# Ponowne uruchomienie aktualizuje skaner w istniejącym kontenerze "siec-ip".
# Stały adres zamiast DHCP:  IP=192.168.1.50/24 GW=192.168.1.1 bash proxmox-lxc.sh
# Inny mostek lub magazyn:   BRIDGE=vmbr1 STORAGE=local-zfs bash proxmox-lxc.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="siec-ip"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nBŁĄD: %s\n' "$*" >&2; exit 1; }
in_ct() { pct exec "$CTID" -- "$@"; }

command -v pct >/dev/null || fail "to nie jest Proxmox (brak polecenia pct)"
[ -f "$HERE/skaner.py" ] || fail "brak pliku skaner.py obok skryptu"

CTID="$(pct list | awk -v n="$NAME" 'NR > 1 && $NF == n { id = $1 } END { print id }')"

if [ -z "$CTID" ]; then
  STORAGE="${STORAGE:-$(pvesm status --content rootdir --enabled 1 | awk 'NR == 2 { print $1 }')}"
  [ -n "$STORAGE" ] || fail "nie znalazłem magazynu na kontenery — podaj go: STORAGE=local-lvm"
  NET="name=eth0,bridge=$BRIDGE,ip=$IP"
  if [ "$IP" != "dhcp" ]; then
    NET="$NET,gw=${GW:?przy stałym IP podaj też bramę, np. GW=192.168.1.1}"
  fi

  say "Pobieram szablon Debiana"
  pveam update >/dev/null
  TEMPLATE="$(pveam available --section system | awk '$2 ~ /^debian-[0-9]+-standard/ { print $2 }' | sort -V | tail -n 1)"
  [ -n "$TEMPLATE" ] || fail "nie znalazłem szablonu Debiana (pveam available)"
  [[ "$(pveam list local)" == *"$TEMPLATE"* ]] || pveam download local "$TEMPLATE"

  CTID="$(pvesh get /cluster/nextid)"
  say "Tworzę kontener $CTID ($NAME) na magazynie $STORAGE, mostek $BRIDGE"
  pct create "$CTID" "local:vztmpl/$TEMPLATE" \
    --hostname "$NAME" --cores 1 --memory 256 --swap 256 \
    --rootfs "$STORAGE:2" --net0 "$NET" \
    --unprivileged 1 --features nesting=1 --onboot 1
  pct start "$CTID"

  say "Czekam na sieć w kontenerze"
  for _ in $(seq 30); do
    in_ct sh -c 'ip route | grep -q default' 2>/dev/null && break
    sleep 2
  done
  in_ct sh -c 'ip route | grep -q default' || fail "kontener nie dostał adresu IP (sprawdź DHCP albo podaj IP=… GW=…)"

  say "Instaluję Pythona w kontenerze"
  in_ct sh -c 'apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 iputils-ping iproute2'
else
  say "Kontener $NAME już istnieje ($CTID) — aktualizuję skaner"
  [[ "$(pct status "$CTID")" == *running* ]] || pct start "$CTID"
fi

say "Kopiuję skaner i uruchamiam usługę"
in_ct mkdir -p /opt/siec-ip
pct push "$CTID" "$HERE/skaner.py" /opt/siec-ip/skaner.py --perms 0644
pct push "$CTID" "$HERE/siec-ip.service" /etc/systemd/system/siec-ip.service --perms 0644
in_ct systemctl daemon-reload
in_ct systemctl enable -q siec-ip
in_ct systemctl restart siec-ip
sleep 3
if ! in_ct systemctl is-active -q siec-ip; then
  # Bez zagnieżdżania (nesting) piaskownica systemd nie wystartuje — wtedy usługa
  # działa jako root kontenera (sam kontener i tak jest odizolowany).
  say "Usługa nie wstała w piaskownicy systemd — uruchamiam ją bez niej"
  in_ct sh -c 'mkdir -p /etc/systemd/system/siec-ip.service.d &&
    printf "[Service]\nDynamicUser=no\n" > /etc/systemd/system/siec-ip.service.d/override.conf &&
    systemctl daemon-reload && systemctl restart siec-ip'
  sleep 3
  if ! in_ct systemctl is-active -q siec-ip; then
    in_ct journalctl -u siec-ip -n 30 --no-pager || true
    fail "usługa siec-ip nie działa — logi powyżej"
  fi
fi

ADDR="$(in_ct hostname -I | awk '{ print $1 }')"
say "Gotowe! Panel: http://$ADDR:8080  (kontener $CTID, pierwszy skan trwa kilka sekund)"
