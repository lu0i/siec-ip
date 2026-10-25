# Skaner sieci domowej — lista adresów IP urządzeń

Mała aplikacja, która co kilka minut przeszukuje Twoją sieć domową, **wyłapuje wszystkie urządzenia** (komputery, telefony, telewizor, drukarkę, kamery, IoT) i **zapamiętuje ich adresy IP** w bazie. Przez przeglądarkę widzisz listę: kto jest teraz online, od kiedy urządzenie jest w sieci, jaki miało adres wcześniej. Nowe, nieznane urządzenia są oznaczone etykietą **„nowe”**.

Jeden plik Pythona, **bez instalowania bibliotek**. Działa na Linuksie (serwer, Raspberry Pi), macOS i Windows. Nic nie wychodzi do internetu.

---

## Jak to działa

```
  serwer w domu                          sieć domowa
  +---------------------+   ping + ARP   +--------------------------+
  |  skaner.py          | -------------> | router, laptop, telefon, |
  |  - skan co 5 min    | <------------- | TV, drukarka, kamera ... |
  |  - baza SQLite      |                +--------------------------+
  |  - panel WWW :8080  | <---- przeglądarka (dowolny komputer/telefon w domu)
  +---------------------+
```

1. Skaner wysyła ping do każdego adresu w sieci (np. `192.168.1.1–254`).
2. Potem czyta tablicę ARP systemu — trafiają tam też urządzenia, które ignorują ping (np. iPhone'y), bo zawsze odpowiadają na zapytanie ARP.
3. Urządzenie jest rozpoznawane po **adresie MAC**, więc gdy router przydzieli mu inny IP, to nadal ten sam wpis — a zmiana ląduje w historii (kliknij adres IP w panelu).
4. Na każdym urządzeniu online sprawdza popularne porty, żeby wiedzieć, **jakie usługi** na nim działają (lista niżej). Z paneli WWW odczytuje tytuł strony, np. „Proxmox Virtual Environment” albo „Home Assistant”.
5. Wszystko zapisuje się w pliku `urzadzenia.db` (SQLite).

## Szybki start (do wypróbowania na Macu)

Dwuklik na **`start.command`** — otworzy się panel w przeglądarce. Albo w terminalu:

```bash
python3 skaner.py
```

Jednorazowy skan z wynikiem w terminalu:

```bash
python3 skaner.py --once
```

## Windows

1. Zainstaluj Pythona z [python.org](https://www.python.org/downloads/windows/) (przy instalacji zaznacz **„Add python.exe to PATH”**) albo ze sklepu Microsoft Store.
2. Skopiuj folder `siec-ip` na komputer i kliknij dwa razy **`start.bat`** — otworzy się okno skanera i panel w przeglądarce.
3. Przy pierwszym uruchomieniu Zapora Windows zapyta o dostęp dla Pythona: zaznacz **Sieci prywatne** i kliknij **Zezwalaj**. Bez tego panel działa tylko na tym komputerze (`http://localhost:8080`), a inne urządzenia w domu go nie otworzą.

Okno skanera musi zostać otwarte — zamknięcie go zatrzymuje skaner. Żeby startował sam po włączeniu komputera, wrzuć skrót do `start.bat` do folderu Autostart (`Win+R` → `shell:startup`).

## Panel WWW

Adres: `http://<IP-serwera>:8080` — z każdego urządzenia w domu.

- **zielona kropka** — urządzenie odpowiedziało w ostatnim skanie,
- **Nazwa** — kliknij i wpisz własną (np. „Telewizor salon”), Enter zapisuje,
- **nowe** — urządzenie pierwszy raz widziane w ciągu ostatnich 24 h,
- **prywatny MAC** — losowy adres MAC, zwykle telefon/tablet z włączonym „prywatnym adresem Wi-Fi” (taki telefon może po czasie pojawić się jako nowe urządzenie),
- **Usługi** — co działa na urządzeniu; niebieskie etykiety to panele WWW, kliknięcie otwiera je w nowej karcie,
- **Eksport CSV** — lista do Excela (otwiera się od razu w kolumnach),
- **Skanuj teraz** — skan bez czekania na kolejny cykl.

### Szukanie usług

Wyszukiwarka przeszukuje też usługi: wpisz `proxmox`, `home assistant`, `ssh`, `drukarka`, `plex` albo numer portu (`8123`), a zostaną tylko urządzenia, na których to działa.

Sprawdzane porty:

| Port | Usługa | Port | Usługa |
|---|---|---|---|
| 21 | FTP | 8000, 8008 | strona WWW |
| 22 | SSH | 8006 | Proxmox |
| 53 | DNS (np. Pi-hole, router) | 8009 | Chromecast |
| 80, 443 | strona WWW (router, NAS, kamera…) | 8080, 8443 | strona WWW |
| 445 | udostępnianie plików (Windows, NAS) | 8096 | Jellyfin |
| 554 | kamera (RTSP) | 8123 | Home Assistant |
| 631 | drukarka (IPP) | 9000, 9443 | strona WWW (np. Portainer) |
| 1883 | MQTT | 9100 | drukarka |
| 3000 | strona WWW (np. Grafana) | 32400 | Plex |
| 3389 | pulpit zdalny Windows | 62078 | iPhone/iPad |
| 5000, 5001 | strona WWW (np. Synology) | | |
| 5900 | VNC | | |
| 7000 | AirPlay | | |

Własny port dopiszesz w słowniku `SERVICES` na początku `skaner.py`.

## Instalacja na Proxmoxie (kontener LXC — zalecane)

Skaner dostaje własny, mały kontener z Debianem (256 MB RAM, 2 GB dysku). Kontener jest podpięty do mostka `vmbr0`, więc widzi całą sieć domową — także sam Proxmox i jego maszyny wirtualne. Na samym hoście Proxmoxa nic się nie instaluje.

**1. Skopiuj folder na Proxmoxa** — w Terminalu na Macu (zamień adres na IP swojego Proxmoxa, zapyta o hasło roota):

```bash
scp -r ~/Desktop/arkusz/siec-ip root@192.168.1.10:/root/
```

**2. Uruchom instalator** — w panelu Proxmoxa wybierz węzeł → **Shell** i wpisz:

```bash
bash /root/siec-ip/proxmox-lxc.sh
```

Skrypt sam pobierze szablon Debiana, utworzy kontener `siec-ip` (z autostartem razem z Proxmoxem), zainstaluje Pythona, uruchomi usługę i na końcu wypisze adres panelu.

**Aktualizacja:** skopiuj nowy `skaner.py` jak w kroku 1 i uruchom ten sam skrypt jeszcze raz — wykryje istniejący kontener i tylko podmieni skaner. Baza urządzeń zostaje.

**Warianty:**

- stały adres kontenera zamiast DHCP (wygodniej, bo adres panelu się nie zmieni):
  `IP=192.168.1.50/24 GW=192.168.1.1 bash /root/siec-ip/proxmox-lxc.sh`
- inny mostek sieciowy lub magazyn: `BRIDGE=vmbr1 STORAGE=local-zfs bash /root/siec-ip/proxmox-lxc.sh`

Logi skanera: `pct exec <numer-kontenera> -- journalctl -u siec-ip -f`

**Ręcznie, bez skryptu:** utwórz w panelu kontener z szablonu Debiana (nieuprzywilejowany, *Nesting* zaznaczone, sieć `vmbr0`), w jego konsoli wpisz `apt install -y python3 iputils-ping iproute2`, a dalej postępuj jak w sekcji poniżej.

## Instalacja na serwerze z Linuksem (usługa systemd)

Wymaga tylko Pythona 3.8+ (jest w każdym Debianie/Ubuntu/Raspberry Pi OS).

```bash
sudo mkdir -p /opt/siec-ip
sudo cp skaner.py /opt/siec-ip/
sudo cp siec-ip.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now siec-ip
```

Sprawdzenie i logi:

```bash
systemctl status siec-ip
journalctl -u siec-ip -f
```

Baza leży w `/var/lib/siec-ip/urzadzenia.db`. Usługa startuje sama po restarcie serwera.

## Instalacja przez Docker (alternatywa)

Na serwerze z Linuksem, w tym folderze:

```bash
docker compose up -d --build
```

Baza zapisuje się w `./dane/urzadzenia.db`. `network_mode: host` jest konieczne — inaczej kontener widzi tylko swoją wewnętrzną sieć, a nie Twoją domową. (Na Docker Desktop dla Maca/Windows to nie zadziała tak samo — tam uruchamiaj skaner bezpośrednio.)

## Opcje

| Opcja | Domyślnie | Opis |
|---|---|---|
| `--subnet 192.168.1.0/24` | wykrywana sama | która sieć ma być skanowana |
| `--interval 300` | 300 s | co ile sekund skanować |
| `--port 8080` | 8080 | port panelu WWW |
| `--host 127.0.0.1` | 0.0.0.0 | `127.0.0.1` = panel dostępny tylko z samego serwera |
| `--db plik.db` | `urzadzenia.db` obok skryptu | gdzie trzymać bazę |
| `--once` | — | jeden skan, wynik w terminalu, koniec |

## Dobrze wiedzieć

- **Bezpieczeństwo:** panel nie ma hasła — każdy w Twojej sieci domowej może go otworzyć. Do domu to zwykle w porządku; nie wystawiaj portu 8080 do internetu na routerze.
- **Wyłączone urządzenie** na Linuksie znika z „online” od razu przy następnym skanie; na macOS i Windows może wisieć jako online jeszcze kilka–kilkanaście minut (tyle system trzyma wpisy ARP).
- **Nazwy hostów** pojawią się tylko, jeśli router je udostępnia przez DNS (wiele routerów to robi, np. `laptop.lan`). Jeśli kolumna jest pusta — nadaj nazwy ręcznie.
- Skaner widzi tylko sieć, w której jest serwer. Jeśli masz osobną sieć dla gości/IoT, uruchom go z `--subnet` dla tamtej sieci (o ile serwer ma do niej dostęp).
- Skan sieci `/24` (254 adresy) trwa kilka–kilkanaście sekund (razem ze sprawdzaniem usług), większej `/21` ok. pół minuty.
- Usługa schowana za zaporą urządzenia (firewall) albo na nietypowym porcie nie pojawi się na liście.
