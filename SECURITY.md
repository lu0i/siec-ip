# Bezpieczeństwo

## Zgłaszanie podatności

Nie zgłaszaj podatności w publicznych zgłoszeniach (Issues). Użyj prywatnego formularza: zakładka **Security → Report a vulnerability** w tym repozytorium. Opisz problem, sposób odtworzenia i wersję programu. Zgłoszenia są obsługiwane w miarę możliwości — to projekt hobbystyczny.

Poprawki bezpieczeństwa trafiają do najnowszej wersji z gałęzi `main` i do kolejnego wydania.

## Model zagrożeń

Skaner jest przeznaczony do **zaufanej sieci domowej lub małej sieci biurowej**, administrowanej przez osobę, która go uruchamia.

| Zakres | Założenie |
|---|---|
| Osoby w sieci lokalnej | Zaufane. Panel nie ma logowania — każdy w sieci może przeglądać listę, zmieniać nazwy, usuwać wpisy i wywołać skan. |
| Internet | Niezaufany. Panel nie może być wystawiony do internetu. |
| Strony WWW otwierane w przeglądarce użytkownika | Niezaufane. Nie mogą odczytać danych z panelu ani wykonać w nim zmian (zob. zabezpieczenia). |
| Skanowane urządzenia | Częściowo niezaufane. Odpowiedzi urządzeń (nazwy hostów, tytuły stron) są traktowane jak dane i escapowane przed wyświetleniem. |

## Zabezpieczenia w aplikacji

- **DNS rebinding** — serwer akceptuje tylko nagłówek `Host` będący adresem IP, nazwą bez kropki albo domeną lokalną (`.lan`, `.local`, `.home`, `.internal`, `.home.arpa`, `.localdomain`). Pozostałe nazwy dostają odpowiedź 403; własną domenę można dopuścić opcją `--allow-host`.
- **CSRF** — żądania zmieniające dane wymagają `Content-Type: application/json` (`POST`) albo metody `DELETE`; przeglądarka wysyła takie żądania z obcej strony dopiero po zapytaniu wstępnym CORS, którego serwer nie zatwierdza.
- **XSS i clickjacking** — wszystkie dane w interfejsie są escapowane; odpowiedzi mają nagłówki `Content-Security-Policy` (m.in. `frame-ancestors 'none'`, `form-action 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` i `Referrer-Policy: no-referrer`. Panel nie ładuje żadnych zewnętrznych zasobów.
- **Baza danych** — wyłącznie zapytania parametryzowane (brak SQL injection).
- **Wejście** — treść żądania ograniczona do 10 kB, nazwy urządzeń do 100 znaków; serwer nie udostępnia plików z dysku.
- **Uprawnienia** — skaner nie wymaga konta administratora. Usługa systemd działa na koncie dynamicznym (`DynamicUser`) z jednym uprawnieniem `CAP_NET_RAW`; instalator Proxmoxa tworzy kontener nieuprzywilejowany.

## Ruch sieciowy

Aplikacja **nie łączy się z internetem**: nie ma telemetrii, kont, sprawdzania aktualizacji ani zewnętrznych bibliotek ładowanych w panelu. Komunikacja obejmuje wyłącznie:

| Ruch | Cel |
|---|---|
| ICMP echo (ping) i ARP | adresy w skanowanej podsieci |
| Połączenia TCP z 28 portami (lista w README) | urządzenia online w podsieci |
| `GET /` po HTTP/HTTPS, maks. 64 kB, limit 3 s | porty WWW wykrytych urządzeń — tylko w celu odczytu tytułu strony |
| Odwrotne zapytania DNS (PTR) | serwer DNS skonfigurowany w systemie, zwykle router |

Przy odczycie tytułów stron:

- przekierowania są wykonywane tylko w obrębie tego samego urządzenia — przekierowanie na inny adres jest ignorowane,
- pomijane są ustawienia proxy systemu,
- weryfikacja certyfikatów TLS jest wyłączona, bo panele w sieci lokalnej (Proxmox, NAS, router) zwykle używają certyfikatów z własnym podpisem; nie są przy tym wysyłane żadne dane logowania ani ciasteczka.

## Przechowywane dane

Plik `urzadzenia.db` (SQLite) zawiera adresy MAC i IP, nazwy hostów, nadane nazwy, otwarte porty, tytuły stron oraz daty wykrycia. To mapa Twojej sieci, więc:

- nie publikuj go — w repozytorium jest wykluczony w `.gitignore`,
- aby usunąć wszystkie dane, wystarczy zatrzymać skaner i skasować ten plik.

## Plik SkanerSieci.exe

Plik budowany jest publicznie z kodu źródłowego tego repozytorium przez [GitHub Actions](.github/workflows/exe.yml) i testowany na Windows przed publikacją. Nie ma płatnego podpisu cyfrowego, dlatego SmartScreen i część programów antywirusowych mogą go oznaczać jako nieznany. Sumę kontrolną SHA-256 każdego wydania znajdziesz w jego opisie:

```powershell
Get-FileHash .\SkanerSieci.exe -Algorithm SHA256
```

Jeśli wolisz nie uruchamiać gotowego pliku, uruchom `skaner.py` bezpośrednio z Pythonem albo zbuduj `.exe` samodzielnie (instrukcja w README).

## Zalecenia

- Nie przekierowuj portu panelu (domyślnie 8080) na routerze. Do zdalnego dostępu użyj VPN.
- Jeśli w Twojej sieci są osoby lub urządzenia, którym nie ufasz (np. sieć gości), uruchom panel z `--host 127.0.0.1` albo ogranicz dostęp do portu zaporą.
- Skanuj tylko sieci, które należą do Ciebie lub którymi administrujesz.
