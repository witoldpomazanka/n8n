print("=== STARTUJEMY APLIKACJĘ ===")
from telethon import TelegramClient, events
import os
from dotenv import load_dotenv
import asyncio
import logging
import aiohttp
import json
from aiohttp import web
import pathlib
from collections import deque
from datetime import datetime
import sys
from telethon.tl.types import User, Channel, Chat
import threading
from queue import Queue
import psycopg2
from psycopg2.extras import RealDictCursor
import asyncpg
import datetime as dt
from datetime import datetime
import dateutil.parser

# Konfiguracja logowania
logger = logging.getLogger('telegram_reader')
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
logging.getLogger('telethon.network.mtprotosender').setLevel(logging.WARNING)

# Ładowanie zmiennych środowiskowych
load_dotenv()

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
PHONE = os.getenv('TELEGRAM_PHONE')
USERNAME = os.getenv('USERNAME')
N8N_WEBHOOK_URL = os.getenv('N8N_WEBHOOK_URL')

# Dane do połączenia z bazą PostgreSQL
PG_USER = os.getenv('POSTGRES_USER')
PG_PASSWORD = os.getenv('POSTGRES_PASSWORD')
PG_DB = os.getenv('POSTGRES_DB')
PG_HOST = os.getenv('POSTGRES_HOST', 'postgres')
PG_PORT = os.getenv('POSTGRES_PORT', '5432')

# Nazwa tabeli dla wiadomości Telegram (bez konfliktu z tabelami n8n)
TELEGRAM_MESSAGES_TABLE = 'telegram_messages_history'

# Globalne zmienne dla procesu autoryzacji
phone_code_hash = None
client = None
db_pool = None

# Bufor na ostatnie wiadomości (max 100)
message_history = deque(maxlen=100)

# Kolejka do komunikacji między wątkami
message_queue = Queue()

# Lista aktywnych połączeń WebSocket
# Teraz może zawierać różne typy WebSocketów (natywne i aiohttp)
websocket_clients = set()

# Funkcja pomocnicza do wysyłania JSON przez różne typy WebSocket
async def send_websocket_json(ws, data):
    """Wysyła dane JSON przez WebSocket niezależnie od jego typu"""
    try:
        # Sprawdzamy, czy to WebSocketResponse (aiohttp)
        if hasattr(ws, 'send_json'):
            await ws.send_json(data)
        # Standardowy WebSocket (websockets)
        else:
            await ws.send(json.dumps(data))
        return True
    except Exception as e:
        logger.error(f"Błąd podczas wysyłania przez WebSocket: {str(e)}")
        return False

logger.info("=== START APLIKACJI ===")
logger.info(f"API_ID: {API_ID}")
logger.info(f"API_HASH: {'*' * len(API_HASH) if API_HASH else 'None'}")
logger.info(f"PHONE: {PHONE}")
logger.info(f"N8N_WEBHOOK_URL: {N8N_WEBHOOK_URL}")
logger.info(f"PG_HOST: {PG_HOST}")
logger.info(f"PG_DB: {PG_DB}")
logger.info("=" * 50)


async def init_database():
    """Inicjalizuje połączenie z bazą danych i tworzy tabelę jeśli nie istnieje"""
    global db_pool
    
    logger.info("=== init_database ===")
    try:
        # Tworzenie puli połączeń
        db_pool = await asyncpg.create_pool(
            user=PG_USER,
            password=PG_PASSWORD,
            database=PG_DB,
            host=PG_HOST,
            port=PG_PORT
        )
        
        logger.info("Połączenie z bazą danych zostało nawiązane")
        
        # Sprawdzenie czy tabela istnieje, jeśli nie to ją tworzymy
        async with db_pool.acquire() as conn:
            table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = $1)",
                TELEGRAM_MESSAGES_TABLE
            )
            
            if not table_exists:
                logger.info(f"Tworzenie tabeli {TELEGRAM_MESSAGES_TABLE}...")
                await conn.execute(f'''
                CREATE TABLE {TELEGRAM_MESSAGES_TABLE} (
                    id SERIAL PRIMARY KEY,
                    message TEXT,
                    chat_id TEXT,
                    chat_title TEXT,
                    chat_type TEXT,
                    sender_id TEXT,
                    sender_name TEXT,
                    timestamp TIMESTAMPTZ,
                    received_at TIMESTAMPTZ,
                    is_new BOOLEAN DEFAULT TRUE
                )
                ''')
                logger.info("Tabela została utworzona pomyślnie")
            else:
                logger.info(f"Tabela {TELEGRAM_MESSAGES_TABLE} już istnieje")
                
                # Sprawdzamy czy kolumna chat_type istnieje
                column_exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_name = $1 AND column_name = 'chat_type')",
                    TELEGRAM_MESSAGES_TABLE
                )
                
                # Jeśli nie istnieje, dodajemy ją
                if not column_exists:
                    logger.info(f"Dodawanie kolumny chat_type do tabeli {TELEGRAM_MESSAGES_TABLE}...")
                    await conn.execute(f"ALTER TABLE {TELEGRAM_MESSAGES_TABLE} ADD COLUMN chat_type TEXT")
                    logger.info("Kolumna chat_type została dodana pomyślnie")
        
        return True
    except Exception as e:
        logger.error(f"Błąd podczas inicjalizacji bazy danych: {str(e)}")
        return False


async def save_message_to_db(message_data):
    """Zapisuje wiadomość do bazy danych"""
    try:
        # Konwersja dat z ISO string do obiektów datetime jeśli są w formie string
        timestamp = message_data['timestamp']
        received_at = message_data['received_at']
        
        if isinstance(timestamp, str):
            timestamp = dateutil.parser.parse(timestamp)
        
        if isinstance(received_at, str):
            received_at = dateutil.parser.parse(received_at)
            
        async with db_pool.acquire() as conn:
            await conn.execute(
                f'''
                INSERT INTO {TELEGRAM_MESSAGES_TABLE} 
                (message, chat_id, chat_title, chat_type, sender_id, sender_name, timestamp, received_at, is_new)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ''',
                message_data['message'],
                message_data['chat_id'],
                message_data['chat_title'],
                message_data.get('chat_type', 'unknown'),  # Obsługa wiadomości bez określonego typu czatu
                message_data['sender_id'],
                message_data['sender_name'],
                timestamp,
                received_at,
                message_data.get('is_new', True)  # Domyślnie nowa wiadomość
            )
            logger.info(f"Wiadomość została zapisana do bazy danych")
    except Exception as e:
        logger.error(f"Błąd podczas zapisywania wiadomości do bazy danych: {str(e)}")
        logger.exception(e)


async def get_latest_message_timestamp():
    """Pobiera timestamp ostatniej wiadomości z bazy danych"""
    try:
        async with db_pool.acquire() as conn:
            latest_timestamp = await conn.fetchval(
                f"SELECT timestamp FROM {TELEGRAM_MESSAGES_TABLE} ORDER BY timestamp DESC LIMIT 1"
            )
            return latest_timestamp
    except Exception as e:
        logger.error(f"Błąd podczas pobierania ostatniego timestampa: {str(e)}")
        return None


async def load_messages_from_db():
    """Ładuje wiadomości z bazy danych do bufora"""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {TELEGRAM_MESSAGES_TABLE} ORDER BY timestamp DESC LIMIT 100"
            )
            
            # Konwertujemy wiersze do słowników
            messages = []
            for row in rows:
                message = dict(row)
                # Formatujemy timestampy do ISO
                message['timestamp'] = message['timestamp'].isoformat()
                message['received_at'] = message['received_at'].isoformat()
                messages.append(message)
            
            # Czyszczenie bufora i dodawanie wiadomości
            message_history.clear()
            for msg in messages:  # Już są posortowane od DESC w zapytaniu SQL
                message_history.append(msg)
            
            logger.info(f"Załadowano {len(messages)} wiadomości z bazy danych")
            return messages
    except Exception as e:
        logger.error(f"Błąd podczas ładowania wiadomości z bazy danych: {str(e)}")
        return []


async def mark_all_messages_as_old():
    """Oznacza wszystkie wiadomości jako stare (nie nowe)"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {TELEGRAM_MESSAGES_TABLE} SET is_new = FALSE"
            )
            logger.info("Wszystkie wiadomości zostały oznaczone jako stare")
    except Exception as e:
        logger.error(f"Błąd podczas oznaczania wiadomości jako stare: {str(e)}")


async def load_historical_messages():
    """Pobiera historyczne wiadomości z Telegrama i zapisuje je do bazy danych"""
    logger.info("=== load_historical_messages ===")
    global client
    
    try:
        # Sprawdzamy ostatni timestamp z bazy danych
        latest_timestamp = await get_latest_message_timestamp()
        
        if latest_timestamp:
            logger.info(f"Ostatnia wiadomość z datą: {latest_timestamp}")
        else:
            logger.info("Brak wiadomości w bazie danych, pobieranie całej historii")
        
        # Licznik dodanych wiadomości
        added_messages = 0
        
        # Iterujemy przez wszystkie dialogi
        async for dialog in client.iter_dialogs():
            # Określamy typ czatu
            chat = dialog.entity
            chat_type = 'unknown'
            
            if isinstance(chat, User):
                chat_type = 'private'
            elif isinstance(chat, Chat):
                chat_type = 'group'
            elif isinstance(chat, Channel):
                if getattr(chat, 'broadcast', False):
                    chat_type = 'channel'
                else:
                    chat_type = 'supergroup'
            
            logger.info(f"Pobieranie wiadomości z dialogu: {dialog.name} (id: {dialog.id}, typ: {chat_type})")
            
            # Pobieramy wiadomości z dialogu (limit 100 per dialog)
            messages_to_process = []
            async for message in client.iter_messages(dialog.id, limit=100):
                # Jeśli mamy najnowszą wiadomość z bazy, pomijamy starsze
                if latest_timestamp and message.date <= latest_timestamp:
                    continue
                
                if message.text:  # Tylko wiadomości tekstowe
                    messages_to_process.append(message)
            
            # Przetwarzamy wiadomości dla danego dialogu
            for message in messages_to_process:
                try:
                    chat = await message.get_chat()
                    sender = await message.get_sender()
                    sender_name = getattr(sender, 'first_name', '') if sender else ''
                    if sender and getattr(sender, 'last_name', None):
                        sender_name += ' ' + sender.last_name
                    
                    message_timezone = message.date.tzinfo
                    
                    message_data = {
                        'message': message.text,
                        'chat_id': str(chat.id),
                        'chat_title': getattr(chat, 'title', None) or getattr(chat, 'username', None) or 'Prywatny',
                        'chat_type': chat_type,
                        'sender_id': str(sender.id if sender else 0),
                        'sender_name': sender_name or 'Nieznany',
                        'timestamp': message.date,  # Używamy obiektu datetime zamiast stringa
                        'received_at': datetime.now(message_timezone),  # Używamy obiektu datetime zamiast stringa
                        'is_new': False  # Historyczne wiadomości nie są nowe
                    }
                    
                    # Zapisujemy wiadomość do bazy danych
                    await save_message_to_db(message_data)
                    added_messages += 1
                except Exception as e:
                    logger.error(f"Błąd podczas przetwarzania wiadomości historycznej: {str(e)}")
                    logger.exception(e)
        
        logger.info(f"Dodano {added_messages} nowych wiadomości do bazy danych")
        
        # Ładujemy wiadomości z bazy do bufora
        await load_messages_from_db()
        
        # Oznaczamy wszystkie wiadomości jako stare
        await mark_all_messages_as_old()
        
        return True
    except Exception as e:
        logger.error(f"Błąd podczas ładowania historycznych wiadomości: {str(e)}")
        logger.exception(e)
        return False


async def send_to_webhook(data):
    logger.info("=== send_to_webhook ===")
    if not N8N_WEBHOOK_URL:
        logger.warning("Brak skonfigurowanego URL dla webhooka (N8N_WEBHOOK_URL)")
        return
    
    logger.info(f"Wysyłanie wiadomości do webhooka: {N8N_WEBHOOK_URL}")
    logger.info(f"Dane wiadomości: {json.dumps(data, ensure_ascii=False)}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(N8N_WEBHOOK_URL, json=data) as response:
                if response.status == 200:
                    logger.info(f"Wiadomość wysłana do webhooka, status: {response.status}")
                    response_text = await response.text()
                    logger.info(f"Odpowiedź z webhooka: {response_text}")
                else:
                    logger.error(f"Błąd podczas wysyłania do webhooka, status: {response.status}")
                    response_text = await response.text()
                    logger.error(f"Odpowiedź z webhooka: {response_text}")
        except aiohttp.ClientConnectorError as e:
            logger.error(f"Błąd połączenia z webhookiem: {str(e)}")
            logger.error(f"Nie można połączyć z {N8N_WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"Błąd podczas wysyłania do webhooka: {str(e)}")
            logger.exception(e)  # Pełny stacktrace


async def broadcast_message(message):
    """Wysyła wiadomość do wszystkich podłączonych klientów WebSocket"""
    if websocket_clients:
        message_json = {
            'type': 'new_message',
            'message': message
        }
        clients_to_remove = set()
        
        for ws in list(websocket_clients):
            try:
                success = await send_websocket_json(ws, message_json)
                if not success:
                    clients_to_remove.add(ws)
            except Exception as e:
                logger.error(f"Błąd podczas wysyłania wiadomości do klienta WebSocket: {str(e)}")
                clients_to_remove.add(ws)
        
        # Usuwamy zepsute połączenia
        for ws in clients_to_remove:
            websocket_clients.discard(ws)
        
        logger.info(f"Wiadomość wysłana do {len(websocket_clients)} klientów WebSocket")


# Handler dla nowych wiadomości
async def handle_new_message(event):
    logger.info("=== handle_new_message ===")
    try:
        chat = await event.get_chat()
        sender = await event.get_sender()
        
        # Określamy typ czatu
        chat_type = 'unknown'
        if isinstance(chat, User):
            chat_type = 'private'
        elif isinstance(chat, Chat):
            chat_type = 'group'
        elif isinstance(chat, Channel):
            if getattr(chat, 'broadcast', False):
                chat_type = 'channel'
            else:
                chat_type = 'supergroup'
        
        sender_name = getattr(sender, 'first_name', '')
        if getattr(sender, 'last_name', None):
            sender_name += ' ' + sender.last_name
        
        # Pobieramy timezone z daty wysłania
        message_timezone = event.date.tzinfo
        
        message_data = {
            'message': event.raw_text,
            'chat_id': str(chat.id),
            'chat_title': getattr(chat, 'title', None) or getattr(chat, 'username', None) or 'Prywatny',
            'chat_type': chat_type,
            'sender_id': str(sender.id if sender else 0),
            'sender_name': sender_name or 'Nieznany',
            'timestamp': event.date,  # Używamy obiektu datetime zamiast stringa
            'received_at': datetime.now(message_timezone),  # Używamy obiektu datetime zamiast stringa
            'is_new': True
        }
        
        # Tworzymy kopię do wyświetlania w logach i wysyłania przez WebSocket
        log_data = message_data.copy()
        log_data['timestamp'] = log_data['timestamp'].isoformat()
        log_data['received_at'] = log_data['received_at'].isoformat()
        
        logger.info(f"""
Nowa wiadomość:
Od: {log_data['sender_name']} ({log_data['sender_id']})
Czat: {log_data['chat_title']} ({log_data['chat_id']})
Typ czatu: {log_data['chat_type']}
Treść: {log_data['message']}
Wysłano: {log_data['timestamp']}
Odebrano: {log_data['received_at']}
""")
        # Zapisujemy wiadomość do bazy danych
        await save_message_to_db(message_data)
        
        # Dodajemy do bufora i wysyłamy przez WebSocket już z datami w formacie ISO
        message_history.append(log_data)
        await broadcast_message(log_data)
        
        # Wysyłamy wiadomość do webhooka
        await send_to_webhook(log_data)
    except Exception as e:
        logger.error(f"Błąd podczas przetwarzania wiadomości: {str(e)}")
        logger.exception(e)  # Dodajemy pełny stacktrace błędu


async def handle_messages(request):
    logger.info("=== handle_messages ===")
    global client
    if not client or not client.is_connected():
        return web.Response(text="Klient nie jest połączony")
    return web.Response(text="OK")


async def get_messages(request):
    logger.info("=== get_messages ===")
    
    try:
        # Pobieramy wiadomości z bazy danych
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {TELEGRAM_MESSAGES_TABLE} ORDER BY timestamp DESC LIMIT 100"
            )
        
        # Konwertujemy wiersze do słowników
        messages = []
        for row in rows:
            message = dict(row)
            # Formatujemy timestampy do ISO
            message['timestamp'] = message['timestamp'].isoformat()
            message['received_at'] = message['received_at'].isoformat()
            messages.append(message)
        
        return web.json_response({'messages': messages})
    except Exception as e:
        logger.error(f"Błąd podczas pobierania wiadomości: {str(e)}")
        return web.json_response({'messages': list(message_history)})


async def index(request):
    logger.info("=== index ===")
    file_path = pathlib.Path(__file__).parent / 'templates' / 'index.html'
    return web.FileResponse(path=file_path)


async def check_session(request):
    logger.info("=== check_session ===")
    global client
    if not client or not client.is_connected():
        await init_client()
    try:
        if not client.is_connected():
            logger.info("Próba połączenia z Telegramem...")
            await client.connect()

        is_authorized = await client.is_user_authorized()
        status = "autoryzowany" if is_authorized else "nieautoryzowany"
        logger.info(f"Status klienta: {status}")
        return web.json_response({'authorized': is_authorized})
    except Exception as e:
        logger.error(f"Błąd podczas sprawdzania sesji: {str(e)}")
        return web.json_response({'authorized': False, 'error': str(e)})


async def request_code(request):
    logger.info("=== request_code ===")
    global phone_code_hash
    try:
        if not client.is_connected():
            await client.connect()
        send_code_result = await client.send_code_request(PHONE)
        phone_code_hash = send_code_result.phone_code_hash
        return web.json_response({'success': True})
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)})


async def verify_code(request):
    logger.info("=== verify_code ===")
    global phone_code_hash
    try:
        data = await request.json()
        code = data.get('code')
        if not code:
            return web.json_response({'success': False, 'error': 'Nie podano kodu'})
        await client.sign_in(PHONE, code, phone_code_hash=phone_code_hash)
        return web.json_response({'success': True})
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)})


async def init_client():
    logger.info("=== init_client ===")
    global client
    session_dir = os.getenv('TELEGRAM_SESSION_DIR', '/data')
    session_file = os.path.join(session_dir, 'telegram_reader_session.session')
    
    logger.info("="*50)
    logger.info("Inicjalizacja klienta Telegram")
    logger.info(f"Katalog sesji: {session_dir}")
    if not os.path.exists(session_dir):
        logger.warning(f"Katalog sesji nie istnieje, tworzę: {session_dir}")
        os.makedirs(session_dir)
    
    if os.path.exists(session_file):
        size = os.path.getsize(session_file)
        logger.info(f"Znaleziono plik sesji (rozmiar: {size} bajtów)")
        logger.info(f"Ścieżka do pliku sesji: {session_file}")
        logger.info("Próba użycia istniejącej sesji...")
    else:
        logger.warning("Nie znaleziono pliku sesji - wymagana będzie autoryzacja")
        logger.info(f"Nowa sesja zostanie utworzona w: {session_file}")
    
    client = TelegramClient(session_file, API_ID, API_HASH)
    
    try:
        await client.connect()
        if await client.is_user_authorized():
            logger.info("✓ Sesja jest aktywna i zautoryzowana")
            me = await client.get_me()
            logger.info(f"Zalogowano jako: {me.first_name} (@{me.username})")
            # Dodajemy handler po pomyślnej inicjalizacji
            client.add_event_handler(handle_new_message, events.NewMessage)
        else:
            logger.warning("✗ Sesja wymaga autoryzacji")
    except Exception as e:
        logger.error(f"Błąd podczas sprawdzania stanu sesji: {str(e)}")
    
    return client


async def reload_messages(request):
    """Endpunkt do ręcznego ponownego załadowania wiadomości"""
    logger.info("=== reload_messages ===")
    global client
    
    if not client or not client.is_connected():
        return web.json_response({'success': False, 'error': 'Klient nie jest połączony'})
        
    try:
        # Pobieranie i zapisywanie historycznych wiadomości
        success = await load_historical_messages()
        
        if success:
            return web.json_response({'success': True, 'message': 'Wiadomości zostały ponownie załadowane'})
        else:
            return web.json_response({'success': False, 'error': 'Błąd podczas ładowania wiadomości'})
    except Exception as e:
        logger.error(f"Błąd podczas ponownego ładowania wiadomości: {str(e)}")
        logger.exception(e)
        return web.json_response({'success': False, 'error': str(e)})


async def main():
    """Główna funkcja programu"""
    global client
    
    logger.info("=== main ===")
    
    # Inicjalizacja klienta Telegram
    client = await init_client()
    
    # Inicjalizacja bazy danych
    success = await init_database()
    if not success:
        logger.error("Nie udało się połączyć z bazą danych - aplikacja używa tylko pamięci")
    else:
        # Pobieranie i zapisywanie historycznych wiadomości
        await load_historical_messages()
    
    # Konfiguracja serwera HTTP
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/messages', get_messages)
    app.router.add_post('/request_code', request_code)
    app.router.add_post('/verify_code', verify_code)
    app.router.add_get('/check_session', check_session)
    app.router.add_get('/reload', reload_messages)  # Nowy endpoint do ponownego ładowania wiadomości
    
    # Obsługa WebSocketa
    async def websocket_route_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        websocket_clients.add(ws)
        logger.info(f"Nowe połączenie WebSocket, aktualnie połączonych: {len(websocket_clients)}")
        
        try:
            # Pobierz wszystkie wiadomości z bazy danych
            messages = await load_messages_from_db()
            
            # Wysyłamy historię wiadomości do klienta WebSocket
            await send_websocket_json(ws, {
                'type': 'history',
                'messages': messages
            })
            
            # Nasłuchujemy na wiadomości od klienta
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    logger.info(f"Otrzymano wiadomość od klienta WebSocket: {msg.data}")
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"Błąd WebSocket: {ws.exception()}")
        except Exception as e:
            logger.error(f"Błąd w obsłudze WebSocket: {str(e)}")
            logger.exception(e)
        finally:
            websocket_clients.discard(ws)
            logger.info(f"Połączenie WebSocket zakończone, pozostałych: {len(websocket_clients)}")
            
        return ws
    
    # Dodanie handlera WebSocket - tylko raz
    app.router.add_get('/ws', websocket_route_handler)
    
    # Uruchomienie serwera HTTP
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Serwer HTTP uruchomiony na http://0.0.0.0:8080")
    logger.info("WebSocket dostępny na ws://0.0.0.0:8080/ws")
    
    # Uruchomienie klienta
    logger.info("Uruchamiam nasłuchiwanie wiadomości...")
    
    try:
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Błąd w głównej funkcji: {str(e)}")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
