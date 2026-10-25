#!/usr/bin/env python3
"""Skaner sieci domowej: wyłapuje urządzenia w sieci LAN i zapamiętuje ich adresy IP.

Bez zewnętrznych bibliotek — wystarczy Python 3.8+.

    python3 skaner.py             # skan co 5 min + panel WWW na porcie 8080
    python3 skaner.py --once      # jeden skan, wynik w terminalu

Urządzenia są rozpoznawane po adresie MAC, więc gdy router (DHCP) przydzieli
komuś nowy IP, to nadal jest ten sam wpis — a zmiana trafia do historii.
"""

import argparse
import concurrent.futures as cf
import csv
import html
import http.client
import io
import ipaddress
import json
import platform
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

SYSTEM = platform.system()
FROZEN = getattr(sys, "frozen", False)  # uruchomiony jako SkanerSieci.exe (PyInstaller)
# W .exe __file__ wskazuje na folder tymczasowy, więc bazę trzymamy obok samego pliku .exe.
HERE = Path(sys.executable if FROZEN else __file__).resolve().parent
MAX_AUTO_HOSTS = 4096   # większe sieci wykryte automatycznie są zawężane do /24
PING_WORKERS = 128
# Na Linuksie wpis ARP urządzenia, które właśnie zniknęło, przez ~5 s ma stan DELAY,
# zanim jądro go sprawdzi i oznaczy jako FAILED. Czekamy, żeby nie liczyć go jako online.
LINUX_ARP_SETTLE = 8
DEAD_ARP_STATES = {"FAILED", "INCOMPLETE", "PROBE"}
PORT_TIMEOUT = 0.6
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: bez migających okien konsoli

# Porty sprawdzane na każdym urządzeniu: port -> (nazwa, schemat linku albo None, ścieżka).
# Dla portów WWW panel pokazuje tytuł strony, więc ogólne "WWW" zwykle zastępuje
# prawdziwa nazwa (np. "Synology DiskStation" na 5000).
SERVICES = {
    21: ("FTP", None, ""),
    22: ("SSH", None, ""),
    53: ("DNS", None, ""),
    80: ("WWW", "http", "/"),
    443: ("WWW", "https", "/"),
    445: ("Pliki (SMB)", None, ""),
    554: ("Kamera (RTSP)", None, ""),
    631: ("Drukarka (IPP)", "http", "/"),
    1883: ("MQTT", None, ""),
    3000: ("WWW", "http", "/"),
    3389: ("Pulpit zdalny (RDP)", None, ""),
    5000: ("WWW", "http", "/"),
    5001: ("WWW", "https", "/"),
    5900: ("VNC", None, ""),
    7000: ("AirPlay", None, ""),
    8000: ("WWW", "http", "/"),
    8006: ("Proxmox", "https", "/"),
    8008: ("WWW", "http", "/"),
    8009: ("Chromecast", None, ""),
    8080: ("WWW", "http", "/"),
    8096: ("Jellyfin", "http", "/"),
    8123: ("Home Assistant", "http", "/"),
    8443: ("WWW", "https", "/"),
    9000: ("WWW", "http", "/"),
    9100: ("Drukarka", None, ""),
    9443: ("WWW", "https", "/"),
    32400: ("Plex", "http", "/web"),
    62078: ("iPhone/iPad", None, ""),
}

MAC_RE = re.compile(r"(?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}")
IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac        TEXT PRIMARY KEY,   -- adres MAC albo "ip:<adres>", gdy MAC nieznany
    ip         TEXT NOT NULL,      -- ostatni znany adres IP
    hostname   TEXT,
    name       TEXT,               -- własna nazwa nadana w panelu
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    online     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ip_history (
    id      INTEGER PRIMARY KEY,
    mac     TEXT NOT NULL,
    ip      TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ip_history_mac ON ip_history (mac);
CREATE TABLE IF NOT EXISTS services (
    mac   TEXT NOT NULL,
    port  INTEGER NOT NULL,
    title TEXT,                    -- tytuł strony, jeśli to panel WWW
    PRIMARY KEY (mac, port)
);
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    network     TEXT NOT NULL,
    found       INTEGER NOT NULL
);
"""


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run(cmd, timeout=10):
    try:
        # errors="replace": polski Windows wypisuje tekst w innym kodowaniu niż oczekuje Python
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                              creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def normalize_mac(mac):
    return ":".join(p.zfill(2) for p in re.split(r"[:-]", mac)).lower()


def is_unicast(mac):
    return mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") and not int(mac[:2], 16) & 1


# ---------- Baza danych ----------

@contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db(path):
    with database(path) as db:
        db.executescript(SCHEMA)


def list_devices(path):
    with database(path) as db:
        devices = [dict(r) for r in db.execute("SELECT * FROM devices")]
        services = db.execute("SELECT mac, port, title FROM services ORDER BY port").fetchall()
        last = db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    by_mac = {}
    for s in services:
        by_mac.setdefault(s["mac"], []).append(s)
    for d in devices:
        d["services"] = [describe_service(d["ip"], s["port"], s["title"]) for s in by_mac.get(d["mac"], [])]
    devices.sort(key=lambda d: ipaddress.ip_address(d["ip"]))
    return devices, dict(last) if last else None


def save_scan(path, found, hostnames, services, network, started):
    ts = now()
    with database(path) as db:
        db.execute("UPDATE devices SET online = 0")
        for key, ip in found.items():
            row = db.execute("SELECT ip FROM devices WHERE mac = ?", (key,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO devices (mac, ip, hostname, first_seen, last_seen, online)"
                    " VALUES (?, ?, ?, ?, ?, 1)",
                    (key, ip, hostnames.get(key), ts, ts),
                )
            else:
                db.execute(
                    "UPDATE devices SET ip = ?, hostname = COALESCE(?, hostname),"
                    " last_seen = ?, online = 1 WHERE mac = ?",
                    (ip, hostnames.get(key), ts, key),
                )
            if row is None or row["ip"] != ip:
                db.execute("INSERT INTO ip_history (mac, ip, seen_at) VALUES (?, ?, ?)", (key, ip, ts))
            db.execute("DELETE FROM services WHERE mac = ?", (key,))
            db.executemany("INSERT INTO services (mac, port, title) VALUES (?, ?, ?)",
                           [(key, port, title) for port, title in services.get(key, [])])
        db.execute(
            "INSERT INTO scans (started_at, finished_at, network, found) VALUES (?, ?, ?, ?)",
            (started, ts, str(network), len(found)),
        )


# ---------- Wykrywanie sieci ----------

def local_ip():
    """Adres IP tego komputera w sieci lokalnej (interfejs z trasą domyślną)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # UDP connect niczego nie wysyła
        return s.getsockname()[0]
    finally:
        s.close()


def interface_info():
    """(ip, długość maski, mac) interfejsu, przez który widać sieć lokalną."""
    ip = local_ip()
    prefix, mac = None, None
    if SYSTEM == "Linux":
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+" + re.escape(ip) + r"/(\d+)",
                      run(["ip", "-o", "-4", "addr", "show"]), re.M)
        if m:
            prefix = int(m.group(2))
            try:
                mac = Path("/sys/class/net/%s/address" % m.group(1).split("@")[0]).read_text().strip()
            except OSError:
                pass
    elif SYSTEM == "Darwin":
        for block in re.split(r"\n(?=\S)", run(["ifconfig"])):
            m = re.search(r"inet " + re.escape(ip) + r" netmask (0x[0-9a-fA-F]+)", block)
            if m:
                prefix = bin(int(m.group(1), 16)).count("1")
                ether = re.search(r"ether (\S+)", block)
                mac = ether.group(1) if ether else None
                break
    elif SYSTEM == "Windows":
        # Opisy w ipconfig zależą od języka systemu, więc szukamy po wartościach, nie po etykietach.
        for block in re.split(r"\n(?=\S)", run(["ipconfig", "/all"])):
            if re.search(r"(?<![\d.])" + re.escape(ip) + r"(?!\d)", block):
                mask = re.search(r"\b255\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", block)
                if mask:
                    prefix = ipaddress.IPv4Network("0.0.0.0/" + mask.group(0)).prefixlen
                ether = MAC_RE.search(block)  # adres fizyczny jest w bloku przed DUID
                mac = ether.group(0) if ether else None
                break
    if mac is None:
        node = uuid.getnode()
        if not node >> 40 & 1:  # bit multicast = uuid nie znalazł prawdziwego MAC
            mac = ":".join("%02x" % (node >> s & 0xFF) for s in range(40, -8, -8))
    return ip, prefix, normalize_mac(mac) if mac else None


def detect_network():
    ip, prefix, _ = interface_info()
    network = ipaddress.ip_network("%s/%s" % (ip, prefix or 24), strict=False)
    if network.num_addresses > MAX_AUTO_HOSTS:
        narrowed = ipaddress.ip_network(ip + "/24", strict=False)
        print("Sieć %s jest duża — skanuję tylko %s (zmień opcją --subnet)." % (network, narrowed))
        network = narrowed
    return network


# ---------- Skanowanie ----------

def ping(ip):
    if SYSTEM == "Windows":
        # Windows zwraca kod 0 także dla „Host docelowy jest nieosiągalny”,
        # więc za odpowiedź uznajemy tylko linię z TTL (jest w każdej wersji językowej).
        return "TTL=" in run(["ping", "-n", "1", "-w", "1000", ip]).upper()
    if SYSTEM == "Darwin":
        cmd = ["ping", "-c", "1", "-t", "1", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_arp_table():
    """{ip: (mac, stan)} z tablicy ARP systemu. Stan znany tylko na Linuksie, inaczej "?"."""
    table = {}
    if SYSTEM == "Linux":
        for line in run(["ip", "-4", "neigh", "show"]).splitlines():
            parts = line.split()
            if "lladdr" in parts:
                table[parts[0]] = (parts[parts.index("lladdr") + 1], parts[-1])
        if not table and Path("/proc/net/arp").exists():  # brak polecenia `ip`
            for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 4 and cols[2] != "0x0":
                    table[cols[0]] = (cols[3], "?")
    else:
        for line in run(["arp", "-a"] if SYSTEM == "Windows" else ["arp", "-an"]).splitlines():
            ip, mac = IP_RE.search(line), MAC_RE.search(line)
            if ip and mac and "incomplete" not in line:
                table[ip.group(1)] = (mac.group(0), "?")
    return {ip: (normalize_mac(mac), state) for ip, (mac, state) in table.items()}


def resolve(ip):
    try:
        name = socket.gethostbyaddr(ip)[0]
    except OSError:
        return None
    return None if name == ip else name


def port_open(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=PORT_TIMEOUT):
            return True
    except OSError:
        return False


def service_url(ip, port):
    _, scheme, path = SERVICES.get(port, (None, None, ""))
    if not scheme:
        return None
    default = (scheme, port) in (("http", 80), ("https", 443))
    return "%s://%s%s%s" % (scheme, ip, "" if default else ":%d" % port, path)


def describe_service(ip, port, title):
    name = SERVICES.get(port, ("port %d" % port,))[0]
    return {"port": port, "name": name, "title": title, "url": service_url(ip, port)}


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Przekierowania tylko w obrębie tego samego urządzenia — nic nie wychodzi do internetu."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_tls = ssl.create_default_context()
_tls.check_hostname = False
_tls.verify_mode = ssl.CERT_NONE  # panele w LAN (Proxmox, NAS, router) mają certyfikaty z własnym podpisem
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPSHandler(context=_tls), SameHostRedirect)


def page_title(url):
    if not url:
        return None
    try:
        try:
            resp = OPENER.open(url, timeout=3)
        except urllib.error.HTTPError as e:  # np. strona logowania z kodem 401 też ma tytuł
            resp = e
        with resp:
            body, charset = resp.read(65536), resp.headers.get_content_charset()
    except (OSError, ValueError, http.client.HTTPException):
        return None
    m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if not m:
        return None
    try:
        text = m.group(1).decode(charset or "utf-8", "replace")
    except LookupError:
        text = m.group(1).decode("utf-8", "replace")
    return " ".join(html.unescape(text).split())[:80] or None


def find_services(pool, found):
    """{klucz: [(port, tytuł strony albo None)]} — otwarte porty z listy SERVICES."""
    probes = [(key, ip, port) for key, ip in found.items() for port in SERVICES]
    opened = [p for p, ok in zip(probes, pool.map(lambda p: port_open(p[1], p[2]), probes)) if ok]
    titles = pool.map(lambda p: page_title(service_url(p[1], p[2])), opened)
    services = {}
    for (key, _, port), title in zip(opened, titles):
        services.setdefault(key, []).append((port, title))
    return services


def scan(db_path, network):
    """Pinguje całą sieć, czyta tablicę ARP, sprawdza usługi i zapisuje wynik. Zwraca {klucz: ip}."""
    started = now()
    own_ip, _, own_mac = interface_info()
    hosts = [str(h) for h in network.hosts()]
    with cf.ThreadPoolExecutor(max_workers=PING_WORKERS) as pool:
        # Ping wymusza zapytanie ARP — urządzenia, które ignorują ping (np. telefony),
        # i tak odpowiadają na ARP, więc trafiają do tablicy ARP.
        replied = {h for h, ok in zip(hosts, pool.map(ping, hosts)) if ok}
        if SYSTEM == "Linux":
            time.sleep(LINUX_ARP_SETTLE)
        arp = {ip: entry for ip, entry in read_arp_table().items()
               if ipaddress.ip_address(ip) in network and is_unicast(entry[0])}

        found = {}
        for ip, (mac, state) in arp.items():
            if ip in replied or state not in DEAD_ARP_STATES:
                found[mac] = ip
        for ip in replied - set(arp) - {own_ip}:
            found["ip:" + ip] = ip
        if ipaddress.ip_address(own_ip) in network:
            found[own_mac or "ip:" + own_ip] = own_ip

        names = {key: pool.submit(resolve, ip) for key, ip in found.items()}
        services = find_services(pool, found)
        hostnames = {key: f.result() for key, f in names.items()}
    save_scan(db_path, found, hostnames, services, network, started)
    return found


class Scanner:
    """Skanuje sieć w tle co `interval` sekund; trigger() wymusza skan od razu."""

    def __init__(self, db_path, network, interval, auto_network=False):
        self.db_path, self.network, self.interval = db_path, network, interval
        self.auto_network = auto_network
        self.busy = False
        self._wake = threading.Event()

    def loop(self):
        while True:
            self.busy = True
            try:
                if self.auto_network:  # np. laptop przełączony na inną sieć Wi-Fi
                    self.network = detect_network()
                found = scan(self.db_path, self.network)
                print("[%s] skan zakończony: %d urządzeń online" % (now(), len(found)))
            except Exception:
                traceback.print_exc()
            finally:
                self.busy = False
            self._wake.wait(self.interval)
            self._wake.clear()

    def trigger(self):
        if not self.busy:
            self._wake.set()


# ---------- Panel WWW ----------

def devices_csv(devices):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # średnik: polski Excel otwiera to od razu w kolumnach
    w.writerow(["Nazwa", "Adres IP", "MAC", "Nazwa hosta", "Usługi", "Status", "Pierwszy raz",
                "Ostatnio widziane"])
    for d in devices:
        w.writerow([
            d["name"] or "", d["ip"], "" if d["mac"].startswith("ip:") else d["mac"], d["hostname"] or "",
            ", ".join("%s (%d)" % (s["title"] or s["name"], s["port"]) for s in d["services"]),
            "online" if d["online"] else "offline",
            d["first_seen"][:16].replace("T", " "), d["last_seen"][:16].replace("T", " "),
        ])
    return ("﻿" + buf.getvalue()).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    scanner = None  # ustawiany w main()

    def log_message(self, *args):
        pass

    def send(self, code, body=b"", content_type="application/json; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=200):
        self.send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def route(self):
        """Ścieżka jako lista segmentów, np. ["api", "devices", "aa:bb:..."]."""
        return [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self):
        db = self.scanner.db_path
        parts = self.route()
        if not parts:
            self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif parts == ["api", "devices"]:
            devices, last = list_devices(db)
            self.send_json({"devices": devices, "last_scan": last, "scanning": self.scanner.busy,
                            "network": str(self.scanner.network), "interval": self.scanner.interval})
        elif parts == ["api", "devices.csv"]:
            self.send(200, devices_csv(list_devices(db)[0]), "text/csv; charset=utf-8",
                      [("Content-Disposition", 'attachment; filename="urzadzenia.csv"')])
        elif len(parts) == 4 and parts[:2] == ["api", "devices"] and parts[3] == "history":
            with database(db) as conn:
                rows = conn.execute("SELECT ip, seen_at FROM ip_history WHERE mac = ? ORDER BY id DESC",
                                    (parts[2],)).fetchall()
            self.send_json([dict(r) for r in rows])
        else:
            self.send_json({"error": "nie znaleziono"}, 404)

    def do_POST(self):
        # Wymóg JSON blokuje wysyłanie poleceń z obcych stron otwartych w przeglądarce.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "wymagany Content-Type: application/json"}, 415)
        parts = self.route()
        if parts == ["api", "scan"]:
            self.scanner.trigger()
            self.send_json({"ok": True}, 202)
        elif len(parts) == 3 and parts[:2] == ["api", "devices"]:
            length = min(int(self.headers.get("Content-Length") or 0), 10000)
            try:
                name = str(json.loads(self.rfile.read(length) or b"{}").get("name", "")).strip()[:100]
            except (ValueError, AttributeError):
                return self.send_json({"error": "niepoprawny JSON"}, 400)
            with database(self.scanner.db_path) as db:
                db.execute("UPDATE devices SET name = ? WHERE mac = ?", (name or None, parts[2]))
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "nie znaleziono"}, 404)

    def do_DELETE(self):
        parts = self.route()
        if len(parts) == 3 and parts[:2] == ["api", "devices"]:
            with database(self.scanner.db_path) as db:
                db.execute("DELETE FROM devices WHERE mac = ?", (parts[2],))
                db.execute("DELETE FROM ip_history WHERE mac = ?", (parts[2],))
                db.execute("DELETE FROM services WHERE mac = ?", (parts[2],))
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "nie znaleziono"}, 404)


PAGE = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Urządzenia w sieci</title>
<style>
:root { --bg:#f5f6f8; --card:#fff; --text:#1c2230; --muted:#6b7280; --line:#e4e7ec;
        --accent:#2563eb; --ok:#16a34a; --off:#b4bac4; --new:#c2410c; --new-bg:#ffedd5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1217; --card:#171b22; --text:#e6e8ec; --muted:#9aa2af; --line:#272d37;
          --accent:#6aa1ff; --ok:#22c55e; --off:#4b5361; --new:#fdba74; --new-bg:#3b2414; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width:1150px; margin:0 auto; padding:28px 16px 48px; }
header { display:flex; flex-wrap:wrap; gap:12px; align-items:flex-end; justify-content:space-between; margin-bottom:18px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-.01em; }
.sub { color:var(--muted); }
.actions, .toolbar, .seg { display:flex; gap:8px; flex-wrap:wrap; }
button, .btn { font:inherit; border:1px solid var(--line); background:var(--card); color:var(--text);
               padding:7px 13px; border-radius:8px; cursor:pointer; text-decoration:none; }
button:hover, .btn:hover { border-color:var(--muted); }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button:disabled { opacity:.65; cursor:default; }
.toolbar { margin-bottom:12px; }
.toolbar input { flex:1; min-width:220px; font:inherit; color:inherit; background:var(--card);
                 border:1px solid var(--line); border-radius:8px; padding:7px 11px; }
.toolbar input:focus { outline:none; border-color:var(--accent); }
.seg { gap:0; }
.seg button { border-radius:0; margin-left:-1px; }
.seg button:first-child { border-radius:8px 0 0 8px; margin-left:0; }
.seg button:last-child { border-radius:0 8px 8px 0; }
.seg button.on { background:var(--text); color:var(--card); border-color:var(--text); position:relative; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow-x:auto; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); font-weight:600; }
tbody tr:last-child td { border-bottom:0; }
.dot { width:9px; height:9px; border-radius:50%; display:inline-block; background:var(--off); }
.dot.on { background:var(--ok); box-shadow:0 0 0 3px color-mix(in srgb, var(--ok) 22%, transparent); }
.mono { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12.5px; }
.muted { color:var(--muted); }
.namecell { display:flex; align-items:center; gap:6px; }
.name { border:1px solid transparent; background:transparent; color:inherit; font:inherit; font-weight:500;
        padding:4px 6px; margin-left:-7px; border-radius:6px; width:190px; }
.name::placeholder { color:var(--muted); font-weight:400; }
.name:hover { border-color:var(--line); }
.name:focus { outline:none; border-color:var(--accent); background:var(--bg); }
.tag { font-size:11px; padding:1px 7px; border-radius:999px; background:var(--bg); color:var(--muted); }
.tag.new { background:var(--new-bg); color:var(--new); font-weight:600; }
.svcs { display:flex; flex-wrap:wrap; gap:4px; white-space:normal; min-width:160px; max-width:360px; }
.svc { display:inline-flex; align-items:center; gap:5px; max-width:240px; font-size:12px; padding:2px 7px;
       border:1px solid var(--line); border-radius:6px; color:var(--text); text-decoration:none; }
.svc .t { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.svc .p { color:var(--muted); font:11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
a.svc { color:var(--accent); }
a.svc:hover { border-color:var(--accent); }
.link { background:none; border:0; padding:0; color:var(--accent); cursor:pointer; }
.del { color:var(--muted); }
.del:hover { color:#dc2626; }
tr.history td { background:var(--bg); white-space:normal; }
.history ul { margin:4px 0; padding-left:18px; }
.empty { padding:44px 16px; text-align:center; color:var(--muted); }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Urządzenia w sieci</h1>
      <div class="sub" id="summary">Wczytywanie…</div>
    </div>
    <div class="actions">
      <a class="btn" href="/api/devices.csv">Eksport CSV</a>
      <button class="primary" id="scan">Skanuj teraz</button>
    </div>
  </header>
  <div class="toolbar">
    <input id="q" type="search" placeholder="Szukaj: nazwa, IP, MAC, usługa (np. proxmox, ssh, 8123)…">
    <div class="seg" id="filter">
      <button data-f="all" class="on">Wszystkie</button>
      <button data-f="online">Online</button>
      <button data-f="offline">Offline</button>
      <button data-f="new">Nowe</button>
    </div>
  </div>
  <div class="card">
    <table>
      <thead><tr>
        <th></th><th>Nazwa</th><th>Adres IP</th><th>Usługi</th><th>MAC</th><th>Nazwa hosta</th>
        <th>Pierwszy raz</th><th>Ostatnio</th><th></th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</main>
<script>
const $ = s => document.querySelector(s);
const state = { devices: [], meta: {}, filter: "all", q: "", open: null, history: {} };
let timer;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNew = d => Date.now() - new Date(d.first_seen) < 864e5;
const realMac = d => !d.mac.startsWith("ip:");
// Telefony i nowsze laptopy używają losowych („prywatnych”) adresów MAC w Wi-Fi.
const privateMac = d => realMac(d) && (parseInt(d.mac.slice(0, 2), 16) & 2);

function when(ts) {
  if (!ts) return "—";
  const s = (Date.now() - new Date(ts)) / 1000;
  if (s < 90) return "przed chwilą";
  if (s < 3600) return Math.round(s / 60) + " min temu";
  if (s < 86400) return Math.round(s / 3600) + " godz. temu";
  return new Date(ts).toLocaleString("pl-PL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
const full = ts => new Date(ts).toLocaleString("pl-PL");

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                               body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

async function load() {
  clearTimeout(timer);
  try {
    const data = await api("GET", "/api/devices");
    state.devices = data.devices;
    state.meta = data;
    render();
    timer = setTimeout(load, data.scanning ? 2000 : 15000);
  } catch (e) {
    $("#summary").textContent = "Brak połączenia z serwerem — ponawiam…";
    timer = setTimeout(load, 5000);
  }
}

function visible() {
  const q = state.q.toLowerCase();
  return state.devices.filter(d => {
    if (state.filter === "online" && !d.online) return false;
    if (state.filter === "offline" && d.online) return false;
    if (state.filter === "new" && !isNew(d)) return false;
    const fields = [d.name, d.ip, d.mac, d.hostname, ...d.services.flatMap(s => [s.name, s.title, String(s.port)])];
    return !q || fields.some(v => (v || "").toLowerCase().includes(q));
  });
}

function service(s) {
  const tip = esc(`${s.name} — port ${s.port}` + (s.title ? `\n${s.title}` : "") + (s.url ? "\nKliknij, aby otworzyć" : ""));
  const inner = `<span class="t">${esc(s.title || s.name)}</span><span class="p">${s.port}</span>`;
  return s.url ? `<a class="svc" href="${esc(s.url)}" target="_blank" rel="noopener" title="${tip}">${inner}</a>`
               : `<span class="svc" title="${tip}">${inner}</span>`;
}

function row(d) {
  const tags = (isNew(d) ? '<span class="tag new">nowe</span>' : "") +
               (privateMac(d) ? '<span class="tag" title="Losowy adres MAC — zwykle telefon lub tablet">prywatny MAC</span>' : "");
  let html = `<tr>
    <td><span class="dot ${d.online ? "on" : ""}" title="${d.online ? "online" : "offline"}"></span></td>
    <td><div class="namecell"><input class="name" data-mac="${esc(d.mac)}" value="${esc(d.name)}"
         placeholder="${esc(d.hostname || d.services.find(s => s.title)?.title || "nadaj nazwę…")}">${tags}</div></td>
    <td><button class="link mono" data-hist="${esc(d.mac)}" title="Pokaż historię adresów IP">${esc(d.ip)}</button></td>
    <td><div class="svcs">${d.services.map(service).join("") || '<span class="muted">—</span>'}</div></td>
    <td class="mono ${realMac(d) ? "" : "muted"}">${realMac(d) ? esc(d.mac) : "nieznany"}</td>
    <td class="${d.hostname ? "" : "muted"}">${esc(d.hostname || "—")}</td>
    <td title="${esc(full(d.first_seen))}">${when(d.first_seen)}</td>
    <td title="${esc(full(d.last_seen))}">${d.online ? "teraz" : when(d.last_seen)}</td>
    <td><button class="link del" data-del="${esc(d.mac)}">Usuń</button></td>
  </tr>`;
  if (state.open === d.mac) {
    const h = state.history[d.mac];
    html += `<tr class="history"><td></td><td colspan="8"><strong>Historia adresów IP</strong>` +
      (h ? `<ul>${h.map(e => `<li><span class="mono">${esc(e.ip)}</span> <span class="muted">od ${esc(full(e.seen_at))}</span></li>`).join("")}</ul>`
         : " <span class='muted'>wczytywanie…</span>") + `</td></tr>`;
  }
  return html;
}

function render() {
  // Nie przerysowuj tabeli, gdy ktoś właśnie wpisuje nazwę urządzenia.
  if (document.activeElement?.classList.contains("name")) return;
  const { devices, meta } = state;
  const online = devices.filter(d => d.online).length;
  const last = meta.last_scan ? when(meta.last_scan.finished_at) : "jeszcze nie było";
  $("#summary").textContent = `${devices.length} urządzeń · ${online} online · sieć ${meta.network}` +
    ` · ostatni skan: ${meta.scanning ? "trwa…" : last}`;
  const btn = $("#scan");
  btn.disabled = meta.scanning;
  btn.textContent = meta.scanning ? "Skanowanie…" : "Skanuj teraz";

  const list = visible();
  $("#rows").innerHTML = list.length ? list.map(row).join("") :
    `<tr><td colspan="9" class="empty">${devices.length ? "Brak urządzeń pasujących do filtra."
      : meta.scanning ? "Trwa pierwszy skan sieci…" : "Brak urządzeń — kliknij „Skanuj teraz”."}</td></tr>`;
}

$("#scan").onclick = async () => {
  await api("POST", "/api/scan", {});
  state.meta.scanning = true;
  render();
  timer = setTimeout(load, 1500);
};
$("#q").oninput = e => { state.q = e.target.value; render(); };
$("#filter").onclick = e => {
  const b = e.target.closest("button");
  if (!b) return;
  state.filter = b.dataset.f;
  document.querySelectorAll("#filter button").forEach(x => x.classList.toggle("on", x === b));
  render();
};
$("#rows").addEventListener("click", async e => {
  const hist = e.target.closest("[data-hist]"), del = e.target.closest("[data-del]");
  if (hist) {
    const mac = hist.dataset.hist;
    state.open = state.open === mac ? null : mac;
    render();
    if (state.open) {
      state.history[mac] = await api("GET", `/api/devices/${encodeURIComponent(mac)}/history`);
      render();
    }
  } else if (del) {
    const d = state.devices.find(x => x.mac === del.dataset.del);
    if (!confirm(`Usunąć ${d.name || d.hostname || d.ip} z listy? Jeśli urządzenie jest w sieci, pojawi się znowu przy następnym skanie.`)) return;
    await api("DELETE", `/api/devices/${encodeURIComponent(d.mac)}`);
    load();
  }
});
$("#rows").addEventListener("change", async e => {
  if (!e.target.classList.contains("name")) return;
  const d = state.devices.find(x => x.mac === e.target.dataset.mac);
  d.name = e.target.value.trim() || null;
  await api("POST", `/api/devices/${encodeURIComponent(d.mac)}`, { name: d.name || "" });
});
$("#rows").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.classList.contains("name")) e.target.blur();
});
load();
</script>
</body>
</html>
"""


# ---------- Uruchomienie ----------

def print_table(db_path):
    devices, _ = list_devices(db_path)
    print("\n%-16s %-18s %-8s %-24s %s" % ("IP", "MAC", "STATUS", "HOST / NAZWA", "USŁUGI"))
    for d in devices:
        print("%-16s %-18s %-8s %-24s %s" % (
            d["ip"], d["mac"] if not d["mac"].startswith("ip:") else "?",
            "online" if d["online"] else "offline", (d["name"] or d["hostname"] or "-")[:24],
            ", ".join("%s:%d" % ((s["title"] or s["name"])[:30], s["port"]) for s in d["services"])))
    print("\nRazem: %d urządzeń, online: %d" % (len(devices), sum(d["online"] for d in devices)))


def main():
    # Windows: gdy wynik idzie do pliku lub logu, polskie litery spoza strony kodowej
    # zamieniają się w "?", zamiast wywracać program.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description="Skaner urządzeń w sieci domowej z panelem WWW.")
    p.add_argument("--subnet", help="sieć do skanowania, np. 192.168.1.0/24 (domyślnie wykrywana sama)")
    p.add_argument("--port", type=int, default=8080, help="port panelu WWW (domyślnie 8080)")
    p.add_argument("--host", default="0.0.0.0",
                   help="adres nasłuchu panelu; 127.0.0.1 = dostęp tylko z tego komputera")
    p.add_argument("--interval", type=int, default=300, help="co ile sekund skanować (domyślnie 300)")
    p.add_argument("--db", default=str(HERE / "urzadzenia.db"), help="plik bazy SQLite")
    p.add_argument("--once", action="store_true", help="wykonaj jeden skan, wypisz wynik i zakończ")
    p.add_argument("--no-browser", action="store_true", help="nie otwieraj panelu w przeglądarce (.exe)")
    args = p.parse_args()

    init_db(args.db)
    network = ipaddress.ip_network(args.subnet, strict=False) if args.subnet else detect_network()
    if network.version != 4:
        p.error("obsługiwane są tylko sieci IPv4")

    if args.once:
        print("Skanuję %s…" % network)
        scan(args.db, network)
        print_table(args.db)
        return

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        raise SystemExit("Nie mogę uruchomić panelu na porcie %d (%s).\n"
                         "Może skaner już działa? Inny port: --port 8081" % (args.port, e))
    scanner = Scanner(args.db, network, args.interval, auto_network=not args.subnet)
    threading.Thread(target=scanner.loop, daemon=True).start()
    Handler.scanner = scanner
    shown = local_ip() if args.host == "0.0.0.0" else args.host
    print("Panel: http://%s:%d   (sieć %s, skan co %d s, baza %s)" % (
        shown, args.port, network, args.interval, args.db))
    if FROZEN:
        print("Zamknij to okno, aby zatrzymać skaner.")
        if not args.no_browser:
            threading.Timer(1, webbrowser.open, ["http://localhost:%d" % args.port]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano.")


def main_frozen():
    """Wariant dla .exe: przy błędzie okno czeka na Enter, zamiast zniknąć, zanim ktoś przeczyta komunikat."""
    try:
        main()
    except SystemExit as e:
        if not e.code:
            return
        if isinstance(e.code, str):
            print(e.code)
        input("\nNaciśnij Enter, aby zamknąć okno…")
    except Exception:
        traceback.print_exc()
        input("\nNaciśnij Enter, aby zamknąć okno…")


if __name__ == "__main__":
    main_frozen() if FROZEN else main()
