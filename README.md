# n8n-telethon-listener

Aplikacja nasłuchująca wiadomości Telegram i przekazująca je do n8n przez webhook.

## Wymagania

- Docker i Docker Compose
- Numer telefonu zarejestrowany w Telegram
- API ID i API Hash z [Telegram API](https://my.telegram.org/apps)

## Konfiguracja

1. Sklonuj repozytorium:
```bash
git clone [URL_REPOZYTORIUM]
cd n8n-telethon-listener
```

2. Skopiuj plik `.env.example` do `.env` i wypełnij zmienne środowiskowe:
```bash
cp .env.example .env
```

3. Edytuj plik `.env` i uzupełnij następujące zmienne:
```
TELEGRAM_API_ID=twoje_api_id
TELEGRAM_API_HASH=twoje_api_hash
TELEGRAM_PHONE=twój_numer_telefonu
N8N_WEBHOOK_URL=http://n8n:5678/webhook/telegram
```

## Instalacja i uruchomienie

1. Zbuduj i uruchom kontenery:
```bash
docker compose up --build
```

2. Alternatywnie, możesz uruchomić kontenery w tle:
```bash
docker compose up -d
```

3. Otwórz w przeglądarce interfejs Telegram Readera: http://localhost:8080

4. Jeśli nie jesteś zalogowany, zostaniesz poproszony o podanie kodu weryfikacyjnego, który otrzymasz SMS-em na swój numer telefonu.

5. Po poprawnym zalogowaniu, aplikacja rozpocznie nasłuchiwanie wiadomości. Nowe wiadomości będą wyświetlane na górze listy i oznaczone kolorem zielonym przez 10 minut.

6. Wszystkie wiadomości będą automatycznie wysyłane do webhooka n8n.

## Zarządzanie kontenerami

1. Zatrzymanie kontenerów:
```bash
docker compose down
```

2. Zatrzymanie kontenerów i usunięcie obrazu (wymusza ponowne zbudowanie):
```bash
docker compose down && docker rmi n8n-telethon-listener
```

3. Ponowne uruchomienie po zmianach:
```bash
docker compose up -d
```

4. Ponowne zbudowanie i uruchomienie po zmianach:
```bash
docker compose up --build
```

5. Sprawdzenie logów:
```bash
docker compose logs -f telethon-listener
```

## Struktura projektu

```
.
├── docker-compose.yml
├── Dockerfile
├── .env
├── .env.example
├── requirements.txt
└── telethon-listener/
    ├── telegram_reader.py
    └── Dockerfile
```

## Konfiguracja n8n

1. Upewnij się, że n8n jest skonfigurowane do nasłuchiwania webhooków na porcie 5678
2. Utwórz nowy workflow w n8n z triggerem typu "Webhook"
3. Skonfiguruj webhook do odbierania wiadomości z Telegrama

## Rozwiązywanie problemów

1. Jeśli kontener nie może się połączyć z Telegramem:
   - Sprawdź, czy API ID i API Hash są poprawne
   - Upewnij się, że numer telefonu jest poprawny
   - Sprawdź połączenie internetowe

2. Jeśli nie otrzymujesz kodu weryfikacyjnego:
   - Upewnij się, że numer telefonu jest poprawny
   - Sprawdź, czy numer jest zarejestrowany w Telegram
   - Spróbuj ponownie po kilku minutach

3. Jeśli webhook nie działa:
   - Sprawdź, czy n8n jest uruchomione
   - Upewnij się, że URL webhooka jest poprawny
   - Sprawdź logi n8n pod kątem błędów

## Licencja

MIT 