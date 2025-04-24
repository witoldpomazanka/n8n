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
import websockets
import threading
from queue import Queue

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

# Globalne zmienne dla procesu autoryzacji
phone_code_hash = None
client = None

# Bufor na ostatnie wiadomości (max 100)
message_history = deque(maxlen=100)

# Kolejka do komunikacji między wątkami
message_queue = Queue()

# Lista aktywnych połączeń WebSocket
websocket_clients = set()

logger.info("=== START APLIKACJI ===")
logger.info(f"API_ID: {API_ID}")
logger.info(f"API_HASH: {'*' * len(API_HASH) if API_HASH else 'None'}")
logger.info(f"PHONE: {PHONE}")
logger.info(f"N8N_WEBHOOK_URL: {N8N_WEBHOOK_URL}")
logger.info("=" * 50)


async def send_to_webhook(data):
    logger.info("=== send_to_webhook ===")
    if not N8N_WEBHOOK_URL:
        return
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(N8N_WEBHOOK_URL, json=data) as response:
                if response.status == 200:
                    logger.info("Wiadomość wysłana do webhooka")
                else:
                    logger.error(f"Błąd podczas wysyłania do webhooka: {response.status}")
        except Exception as e:
            logger.error(f"Błąd podczas wysyłania do webhooka: {str(e)}")


async def websocket_handler(websocket, path):
    """Obsługa połączeń WebSocket"""
    websocket_clients.add(websocket)
    try:
        # Wysyłamy historię wiadomości nowemu klientowi
        await websocket.send(json.dumps({
            'type': 'history',
            'messages': list(message_history)
        }))
        
        # Nasłuchujemy na wiadomości od klienta
        async for message in websocket:
            # Możemy dodać obsługę wiadomości od klienta jeśli będzie potrzebna
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        websocket_clients.remove(websocket)


async def broadcast_message(message):
    """Wysyła wiadomość do wszystkich podłączonych klientów WebSocket"""
    if websocket_clients:
        message_json = json.dumps({
            'type': 'new_message',
            'message': message
        })
        await asyncio.gather(
            *[client.send(message_json) for client in websocket_clients],
            return_exceptions=True
        )


# Handler dla nowych wiadomości
async def handle_new_message(event):
    logger.info("=== handle_new_message ===")
    try:
        chat = await event.get_chat()
        sender = await event.get_sender()
        sender_name = getattr(sender, 'first_name', '')
        if getattr(sender, 'last_name', None):
            sender_name += ' ' + sender.last_name
        
        # Pobieramy timezone z daty wysłania
        message_timezone = event.date.tzinfo
        
        message_data = {
            'message': event.raw_text,
            'chat_id': str(chat.id),
            'chat_title': getattr(chat, 'title', None),
            'sender_id': str(sender.id),
            'sender_name': sender_name,
            'timestamp': event.date.isoformat(),
            'received_at': datetime.now(message_timezone).isoformat()
        }
        logger.info(f"""
Nowa wiadomość:
Od: {message_data['sender_name']} ({message_data['sender_id']})
Czat: {message_data['chat_title'] or 'Prywatny'} ({message_data['chat_id']})
Treść: {message_data['message']}
Wysłano: {message_data['timestamp']}
Odebrano: {message_data['received_at']}
""")
        message_history.append(message_data)
        # Wysyłamy wiadomość do wszystkich podłączonych klientów WebSocket
        await broadcast_message(message_data)
        # await send_to_webhook(message_data)  # Zakomentowane do czasu implementacji webhooka
    except Exception as e:
        logger.error(f"Błąd podczas przetwarzania wiadomości: {str(e)}")


async def handle_messages(request):
    logger.info("=== handle_messages ===")
    global client
    if not client or not client.is_connected():
        return web.Response(text="Klient nie jest połączony")
    return web.Response(text="OK")


async def get_messages(request):
    logger.info("=== get_messages ===")
    global client
    if not client or not client.is_connected():
        return web.json_response({'messages': []})
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
    
    logger.info("="*50)


async def start_websocket_server():
    """Uruchamia serwer WebSocket"""
    server = await websockets.serve(websocket_handler, '0.0.0.0', 8765)
    logger.info("Serwer WebSocket uruchomiony na porcie 8765")
    return server


async def main():
    logger.info("=== main ===")
    await init_client()

    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/check_session', check_session)
    app.router.add_post('/request_code', request_code)
    app.router.add_post('/verify_code', verify_code)
    app.router.add_get('/handle_messages', handle_messages)
    app.router.add_get('/messages', get_messages)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()

    logger.info("Serwer uruchomiony na http://0.0.0.0:8080")

    try:
        # Uruchomienie serwera WebSocket
        websocket_server = await start_websocket_server()

        # Uruchomienie klienta
        logger.info("Uruchamiam nasłuchiwanie wiadomości...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Błąd w głównej funkcji: {str(e)}")
    finally:
        await runner.cleanup()
        if client:
            await client.disconnect()
        logger.info("Aplikacja zakończona")


if __name__ == '__main__':
    asyncio.run(main())
