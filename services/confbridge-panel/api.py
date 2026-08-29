#!/usr/bin/env python3
"""
Asterisk Conference Panel API v4.0
Чистовая пересборка: все накопленные фиксы + автозапись + история + адресная книга
"""

import socket
import threading
import time
import sqlite3
import hashlib
import os
import secrets
import subprocess
from datetime import datetime
from typing import List
from dataclasses import dataclass, field
from urllib.parse import unquote
from flask import Flask, jsonify, request, send_from_directory, session, g, send_file
# flask_cors больше не нужен — всё идёт через один и тот же origin
# (портал/nginx), кросс-доменных запросов больше нет.
from functools import wraps

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

INSTALL_DIR = '/opt/asterisk-panel'
SECRET_KEY_FILE = os.path.join(INSTALL_DIR, '.secret_key')
DB_PATH = os.path.join(INSTALL_DIR, 'panel.db')
RECORDINGS_DIR = '/var/spool/asterisk/monitor'

import re

# Отслеживание статуса набора номеров при приглашении в конференцию —
# ключ: ActionID отправленного Originate, значение: словарь со статусом.
# Статус 'ringing' пока идёт набор, дальше меняется на 'answered'/'failed'
# по событию OriginateResponse (см. AutoRecordListener). Не отвеченные
# звонки показываются во вкладке "Приглашение" отдельным блоком снизу
# с кнопкой повторного набора — как в старой панели Yeastar.
invite_tracker = {}
invite_tracker_lock = threading.Lock()

def get_freepbx_extensions():
    """
    Читает реальный список добавочных номеров прямо из базы FreePBX
    (таблица users: extension, name) — то же самое, что видно в
    Applications -> Extensions. Так же устроен выбор номеров в старой
    панели Yeastar: список берётся из базы АТС, а не набивается вручную.

    Учётные данные MySQL не хранятся в этом файле — читаются из
    /etc/freepbx.conf, как это делает сам FreePBX.
    """
    try:
        with open('/etc/freepbx.conf', 'r', encoding='utf-8', errors='ignore') as f:
            conf = f.read()

        def _extract(key, default=''):
            # FreePBX может писать и ключ, и значение как в одинарных,
            # так и в двойных кавычках — например $amp_conf["AMPDBUSER"]
            m = re.search(r"\$amp_conf\[['\"]" + key + r"['\"]\]\s*=\s*['\"]([^'\"]*)['\"]", conf)
            return m.group(1) if m else default

        db_user = _extract('AMPDBUSER', 'freepbxuser')
        db_pass = _extract('AMPDBPASS', '')
        db_host = _extract('AMPDBHOST', 'localhost')
        db_name = _extract('AMPDBNAME', 'asterisk')

        cmd = ['mysql', '-N', '-B', '-h', db_host, '-u', db_user]
        if db_pass:
            # Флаг -p БЕЗ значения заставляет mysql ждать пароль в
            # интерактивном вводе и виснуть — добавляем его только когда
            # пароль реально есть.
            cmd.append(f'-p{db_pass}')
        cmd += [db_name, '-e', 'SELECT extension, name FROM users ORDER BY CAST(extension AS UNSIGNED)']

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print(f"[Extensions] ⚠️ Ошибка чтения из БД FreePBX: {result.stderr.strip()}")
            return []

        extensions = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('\t')
            if len(parts) >= 2:
                extensions.append({'number': parts[0].strip(), 'name': parts[1].strip() or parts[0].strip()})
        return extensions
    except Exception as e:
        print(f"[Extensions] ⚠️ Не удалось получить список добавочных: {e}")
        return []

def get_or_create_secret_key():
    try:
        if os.path.exists(SECRET_KEY_FILE):
            with open(SECRET_KEY_FILE, 'r') as f:
                return f.read().strip()
        key = secrets.token_hex(32)
        os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
        with open(SECRET_KEY_FILE, 'w') as f:
            f.write(key)
        os.chmod(SECRET_KEY_FILE, 0o600)
        return key
    except Exception:
        return secrets.token_hex(32)

CONFIG = {
    'asterisk_host': '127.0.0.1',
    'asterisk_port': 5038,
    'ami_username': 'dashboard',
    'ami_secret': 'CHANGE_ME',  # ⚠️ ВАЖНО при повторном деплое: если install.sh
    # уже когда-то подставил сюда реальный секрет AMI-пользователя dashboard —
    # не копируйте этот файл поверх живого api.py вслепую, сначала сверьте
    # это значение с /etc/asterisk/manager_custom.conf ([dashboard] secret=...)
    # и перенесите его сюда вручную. Иначе полная замена файла тихо откатит
    # секрет обратно на плейсхолдер, и AMI перестанет подключаться.
    'api_host': '127.0.0.1',
    'api_port': 5000,
    'ami_timeout': 10,
    'ami_retries': 5,
    'ami_retry_delay': 3,
    'db_path': DB_PATH,
    'recordings_dir': RECORDINGS_DIR,
    'secret_key': get_or_create_secret_key(),
}

# Права упрощены под реальный функционал панели (без блокировки конференций,
# без ручного управления записью, без раздела "Пользователи").
ROLES = {
    'admin':    {'name': 'Администратор', 'permissions': ['view', 'mute', 'kick', 'invite', 'history', 'recordings']},
    'manager':  {'name': 'Менеджер',       'permissions': ['view', 'mute', 'kick', 'invite', 'history', 'recordings']},
    'operator': {'name': 'Оператор',       'permissions': ['view', 'mute', 'invite', 'history']},
    'viewer':   {'name': 'Наблюдатель',    'permissions': ['view']},
}

# =============================================================================
# БАЗА ДАННЫХ
# =============================================================================

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(CONFIG['db_path'])
        g.db.row_factory = sqlite3.Row
    return g.db

def init_db():
    os.makedirs(os.path.dirname(CONFIG['db_path']), exist_ok=True)
    db = sqlite3.connect(CONFIG['db_path'])
    cursor = db.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'operator',
        full_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP,
        is_active BOOLEAN DEFAULT 1
    )''')

    # История подключений/отключений участников (заполняется автослушателем
    # AMI-событий ConfbridgeJoin/ConfbridgeLeave) — вкладка "История".
    cursor.execute('''CREATE TABLE IF NOT EXISTS conference_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conference TEXT,
        event TEXT NOT NULL,
        caller_id_num TEXT,
        caller_id_name TEXT,
        event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Адресная книга для быстрого набора во вкладке "Приглашение".
    cursor.execute('''CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        number TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Модераторы — заходят в конференцию сразу со звуком (unmute).
    # Все остальные участники по умолчанию подключаются заглушенными,
    # администратор включает звук вручную по просьбе (как в Yeastar).
    cursor.execute('''CREATE TABLE IF NOT EXISTS moderators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        number TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Конференц-комнаты, которые панель предлагает к выбору, даже когда
    # в них сейчас никого нет (номер + название). Настраивается через
    # веб-интерфейс — ничего не зашито в коде.
    cursor.execute('''CREATE TABLE IF NOT EXISTS conference_rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        action TEXT,
        target TEXT,
        ip_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        admin_password = secrets.token_urlsafe(8)
        admin_hash = hashlib.sha256(admin_password.encode()).hexdigest()
        cursor.execute('INSERT INTO users (username, password_hash, role, full_name) VALUES (?, ?, ?, ?)',
                      ('admin', admin_hash, 'admin', 'Администратор системы'))
        password_file = os.path.join(INSTALL_DIR, 'admin_password.txt')
        with open(password_file, 'w') as f:
            f.write(f"Логин: admin\nПароль: {admin_password}\n")
        os.chmod(password_file, 0o600)
        print(f"\n[DB] ✅ Пароль admin: {admin_password}")

    db.commit()
    db.close()
    print("[DB] ✅ База данных инициализирована")

def log_history(conference, event, num, name):
    """Пишет событие подключения/отключения. Вызывается из фонового
    потока автослушателя, поэтому открывает собственное соединение с БД
    (нет доступа к flask.g вне контекста запроса).

    Время пишем явно локальное (datetime.now()), а не полагаемся на
    CURRENT_TIMESTAMP по умолчанию в SQLite — та колонка всегда в UTC,
    что даёт разницу в несколько часов с реальным местным временем
    сервера."""
    try:
        conn = sqlite3.connect(CONFIG['db_path'], timeout=5)
        conn.execute(
            'INSERT INTO conference_history (conference, event, caller_id_num, caller_id_name, event_time) VALUES (?, ?, ?, ?, ?)',
            (conference, event, num, name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[History] ⚠️ Не удалось записать событие: {e}")

def is_moderator(number):
    """Проверка по номеру — модератор или обычный участник. Тоже открывает
    отдельное соединение (вызывается из фонового потока слушателя)."""
    try:
        conn = sqlite3.connect(CONFIG['db_path'], timeout=5)
        cursor = conn.execute('SELECT 1 FROM moderators WHERE number = ?', (number,))
        found = cursor.fetchone() is not None
        conn.close()
        return found
    except Exception as e:
        print(f"[Moderators] ⚠️ Не удалось проверить номер: {e}")
        return False

def get_conference_room_names():
    """Словарь номер -> название для настроенных конференц-комнат.
    Используется при построении списка активных конференций (для подписи),
    поэтому открывает собственное соединение — как и другие функции,
    вызываемые вне контекста Flask-запроса."""
    try:
        conn = sqlite3.connect(CONFIG['db_path'], timeout=5)
        cursor = conn.execute('SELECT number, name FROM conference_rooms')
        result = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()
        return result
    except Exception as e:
        print(f"[ConferenceRooms] ⚠️ Не удалось прочитать список комнат: {e}")
        return {}

# =============================================================================
# МОДЕЛИ
# =============================================================================

@dataclass
class Participant:
    channel: str = ""
    caller_id_name: str = ""
    caller_id_num: str = ""
    admin: bool = False
    muted: bool = False
    marked: bool = False

@dataclass
class Conference:
    conference: str = ""
    name: str = ""
    parties: int = 0
    recorded: bool = False
    participants: List[Participant] = field(default_factory=list)

# =============================================================================
# AMI КЛИЕНТ
#
# Общий клиент для REST-команд (mute/kick/invite/...). Все обращения к
# сокету защищены threading.Lock — иначе при параллельных HTTP-запросах
# (Flask threaded=True) ответы разных команд перемешиваются и всё виснет.
#
# Разбор ответа читает AMI-пакетами (до пустой строки-границы), а не
# построчно до первой пустой строки: Asterisk может вклинить в поток
# незапрошенные Event-пакеты (FullyBooted, VarSet, ConfbridgeRecord и т.п.,
# особенно когда в конференции есть активный звонок) МЕЖДУ отправкой
# команды и её фактическим Response — их нужно пропускать, а не принимать
# за конец ответа.
# =============================================================================

class AMIClient:
    def __init__(self, host, port, username, secret):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.socket = None
        self.connected = False
        self.lock = threading.Lock()

    def connect(self):
        for attempt in range(CONFIG['ami_retries']):
            try:
                print(f"[AMI] Попытка {attempt + 1}/{CONFIG['ami_retries']}...")
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.settimeout(CONFIG['ami_timeout'])
                self.socket.connect((self.host, self.port))

                banner = self._read_line()
                if not banner or 'Asterisk Call Manager' not in banner:
                    self.socket.close()
                    time.sleep(CONFIG['ami_retry_delay'])
                    continue

                # Events: off — это соединение используется ТОЛЬКО для
                # прямых команд/ответов (список конференций, mute/kick,
                # запись, приглашения), а не для чтения потока событий.
                # Без этого флага Asterisk шлёт сюда вперемешку с нашими
                # ответами ещё и все посторонние события (ConfbridgeJoin/
                # Leave от чужой активности, OriginateResponse от массовых
                # приглашений и т.д.) — из-за этого разбор ответа может
                # запутаться в потоке и зависнуть надолго. Результаты
                # списочных команд (ConfbridgeList и т.п.) это НЕ
                # затрагивает — они приходят как часть прямого ответа на
                # команду независимо от этого флага.
                auth = self._send_action('Login', {'Username': self.username, 'Secret': self.secret, 'Events': 'off'})
                if auth and auth.get('Response') == 'Success':
                    self.connected = True
                    print(f"[AMI] ✅ Подключено к {self.host}:{self.port}")
                    return True
                self.socket.close()
                time.sleep(CONFIG['ami_retry_delay'])
            except Exception as e:
                print(f"[AMI] ❌ Ошибка: {e}")
                if self.socket:
                    try: self.socket.close()
                    except Exception: pass
                time.sleep(CONFIG['ami_retry_delay'])
        return False

    def disconnect(self):
        try:
            if self.socket:
                self._send_action('Logoff')
                self.socket.close()
            self.connected = False
        except Exception:
            pass

    def _read_line(self):
        if not self.socket:
            return ""
        data = b""
        while True:
            try:
                byte = self.socket.recv(1)
                if not byte or byte == b'\n':
                    break
                data += byte
            except Exception:
                break
        return data.decode('utf-8', errors='ignore').strip()

    def _read_packet_raw(self):
        """Один AMI-пакет (строки key: value до пустой строки-границы)."""
        packet = {}
        while True:
            line = self._read_line()
            if line == "":
                break
            if ':' in line:
                k, v = line.split(':', 1)
                packet[k.strip()] = v.strip()
        return packet

    def _send_action(self, action, params=None):
        with self.lock:
            if not self.socket:
                return {}
            req = f"Action: {action}\r\n"
            if params:
                for k, v in params.items():
                    req += f"{k}: {v}\r\n"
            req += "\r\n"
            try:
                self.socket.sendall(req.encode('utf-8'))
            except Exception:
                return {}

            response = {}
            guard = 0
            while guard < 100:
                guard += 1
                packet = self._read_packet_raw()
                if not packet:
                    break
                if 'Response' not in packet:
                    continue
                response = packet
                if response.get('Response') == 'Success':
                    if action in ('ConfbridgeList', 'ConfbridgeListRooms'):
                        while True:
                            line = self._read_line()
                            if not line or ('EventList' in line and 'Complete' in line):
                                break
                break
            return response

    def send_action(self, action, params=None):
        if not self.connected:
            return {}
        return self._send_action(action, params)

# =============================================================================
# МЕНЕДЖЕР КОНФЕРЕНЦИЙ
# =============================================================================

class ConferenceManager:
    def __init__(self, ami, invite_ami=None):
        self.ami = ami
        # Приглашения (особенно массовые — десятки Originate подряд) идут
        # через ОТДЕЛЬНОЕ AMI-соединение, а не то, что опрашивает
        # /api/conferences. Иначе цепочка из N звонков держит общий Lock
        # секундами, и весь остальной интерфейс панели зависает (pending)
        # на всё это время — та же проблема, что была со стартом/стопом
        # автозаписи, и то же решение: разделить соединения.
        self.invite_ami = invite_ami or ami

    def _read_packet(self):
        packet = {}
        while True:
            line = self.ami._read_line()
            if line == "":
                break
            if ':' in line:
                k, v = line.split(':', 1)
                packet[k.strip()] = v.strip()
        return packet

    def get_all_conferences(self):
        if not self.ami.connected or not self.ami.socket:
            return []

        room_names = get_conference_room_names()
        conferences = {}
        try:
            with self.ami.lock:
                self.ami.socket.sendall(b"Action: ConfbridgeListRooms\r\n\r\n")

                ack = self._read_packet()
                if ack.get('Response') == 'Error':
                    print(f"[Error] ConfbridgeListRooms отклонён AMI: {ack.get('Message')}")
                    return []

                guard = 0
                while guard < 5000:
                    guard += 1
                    packet = self._read_packet()
                    if not packet:
                        break
                    event = packet.get('Event', '')
                    if event == 'ConfbridgeListRoomsComplete':
                        break
                    if event == 'ConfbridgeListRooms' and packet.get('Conference'):
                        c = packet['Conference']
                        parties_raw = packet.get('Parties', '0')
                        conferences[c] = Conference(
                            conference=c,
                            name=room_names.get(c, f'Конференция {c}'),
                            parties=int(parties_raw) if str(parties_raw).isdigit() else 0,
                            recorded=packet.get('Recorded', 'No') == 'Yes',
                        )

                for conf in list(conferences.keys()):
                    self.ami.socket.sendall(
                        f"Action: ConfbridgeList\r\nConference: {conf}\r\n\r\n".encode()
                    )
                    ack = self._read_packet()
                    if ack.get('Response') == 'Error':
                        continue

                    guard = 0
                    while guard < 5000:
                        guard += 1
                        packet = self._read_packet()
                        if not packet:
                            break
                        event = packet.get('Event', '')
                        if event == 'ConfbridgeListComplete':
                            break
                        if event == 'ConfbridgeList' and packet.get('Channel'):
                            conferences[conf].participants.append(Participant(
                                channel=packet.get('Channel', ''),
                                caller_id_num=packet.get('CallerIDNum', ''),
                                caller_id_name=packet.get('CallerIDName') or packet.get('CallerIDNum', ''),
                                admin=packet.get('Admin', 'No') == 'Yes',
                                muted=packet.get('Muted', 'No') == 'Yes',
                                marked=packet.get('MarkedUser', 'No') == 'Yes',
                            ))
                    conferences[conf].parties = len(conferences[conf].participants)
        except Exception as e:
            print(f"[Error] get_all_conferences: {e}")

        return list(conferences.values())

    def mute_participant(self, conf, channel):
        return self.ami.send_action('ConfbridgeMute', {'Conference': conf, 'Channel': channel}).get('Response') == 'Success'

    def unmute_participant(self, conf, channel):
        return self.ami.send_action('ConfbridgeUnmute', {'Conference': conf, 'Channel': channel}).get('Response') == 'Success'

    def kick_participant(self, conf, channel):
        return self.ami.send_action('ConfbridgeKick', {'Conference': conf, 'Channel': channel}).get('Response') == 'Success'

    def invite_participant(self, conf, number, name=None):
        """Технология канала — PJSIP (все номера в этой АТС именно PJSIP,
        перенесены с Yeastar); 'SIP/<номер>' канал не найдёт.

        Звонок отправляется в АСИНХРОННОМ режиме (Async: true) с уникальным
        ActionID. Немедленный ответ AMI означает только "звонок поставлен
        в очередь на набор", а не что абонент ответил — реальный результат
        (ответил/не ответил/занято/ошибка) приходит позже отдельным
        событием OriginateResponse, которое слушает AutoRecordListener и
        обновляет invite_tracker. Это и даёт эффект "провала вниз списка"
        как в Yeastar: пока не пришёл ответ — статус "ringing", как только
        пришёл отрицательный результат — статус "failed" с возможностью
        повторного набора."""
        action_id = f"invite-{int(time.time()*1000)}-{number}"
        with invite_tracker_lock:
            invite_tracker[action_id] = {
                'action_id': action_id,
                'conference': conf,
                'number': number,
                'name': name or number,
                'status': 'ringing',
                'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
        result = self.invite_ami.send_action('Originate', {
            'Channel': f'PJSIP/{number}',
            'Context': 'from-internal',
            'Exten': conf,
            'Priority': '1',
            'CallerID': get_conference_room_names().get(conf, f'Конференция {conf}'),
            'Async': 'true',
            'ActionID': action_id,
        })
        if result.get('Response') != 'Success':
            # Даже поставить в очередь не удалось (например, канал занят
            # набором другого номера с тем же ActionID) — сразу помечаем.
            with invite_tracker_lock:
                if action_id in invite_tracker:
                    invite_tracker[action_id]['status'] = 'failed'
        return result.get('Response') == 'Success'

    def mass_invite(self, conf, numbers, names=None):
        names = names or {}
        return [{'number': n, 'success': self.invite_participant(conf, n, names.get(n))} for n in numbers]

# =============================================================================
# АВТОЗАПИСЬ + ИСТОРИЯ ПОДКЛЮЧЕНИЙ
#
# Три полностью независимых AMI-соединения работают параллельно:
#   1. ami_client       — REST-команды панели (mute/kick/invite/список)
#   2. record_client     — ТОЛЬКО команды старт/стоп записи
#   3. events-соединение — ТОЛЬКО чтение событий (внутри этого класса)
#
# Если бы старт/стоп записи шли через тот же ami_client, что и обычные
# запросы /api/conferences, они делили бы один Lock — а Asterisk может
# отвечать на ConfbridgeStopRecord не мгновенно (дописывает файл), и на
# это время весь интерфейс панели зависал бы в ожидании. Отдельное
# соединение для записи полностью убирает эту зависимость.
#
# Слушает ConfbridgeJoin/ConfbridgeLeave:
#   - первый участник в комнате -> запись стартует автоматически
#   - последний участник вышел -> запись останавливается автоматически
#   - каждое событие пишется в conference_history для вкладки "История"
# =============================================================================

class AutoRecordListener:
    def __init__(self, conference_manager: ConferenceManager, host, port, username, secret):
        self.conference_manager = conference_manager
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.socket = None
        self.room_counts = {}
        self.counts_lock = threading.Lock()
        self.running = False
        # Отдельный AMI-клиент только для команд записи — свой Lock,
        # свой сокет, никак не пересекается с REST-трафиком панели.
        self.record_client = AMIClient(host, port, username, secret)

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _connect(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(CONFIG['ami_timeout'])
            sock.connect((self.host, self.port))
            self.socket = sock

            banner = self._read_line()
            if not banner or 'Asterisk Call Manager' not in banner:
                self.socket.close()
                self.socket = None
                return False

            self._send_raw({'Action': 'Login', 'Username': self.username, 'Secret': self.secret})
            resp = self._read_packet()
            if resp.get('Response') != 'Success':
                self.socket.close()
                self.socket = None
                return False

            # После логина событийный сокет не должен считать паузу без
            # событий обрывом связи — Asterisk может молчать минутами,
            # если никто не подключён. Таймаут нужен только на этапе
            # подключения (уже отработал выше), дальше ждём бесконечно.
            self.socket.settimeout(None)

            print("[AutoRecord] ✅ Событийное соединение AMI установлено")

            if not self.record_client.connected:
                self.record_client.connect()

            self._reconcile_active_rooms()
            return True
        except Exception as e:
            print(f"[AutoRecord] ❌ Ошибка подключения: {e}")
            self.socket = None
            return False

    def _reconcile_active_rooms(self):
        """При старте (или переподключении) панели могут уже идти звонки —
        для них ConfbridgeJoin не придёт повторно. Досчитываем текущее
        число участников и включаем запись, если она ещё не идёт."""
        try:
            confs = self.conference_manager.get_all_conferences()
            with self.counts_lock:
                for c in confs:
                    self.room_counts[c.conference] = c.parties
                    if c.parties > 0:
                        self._start_recording(c.conference)
        except Exception as e:
            print(f"[AutoRecord] ⚠️ Реконсиляция не удалась: {e}")

    def _read_line(self):
        if not self.socket:
            return ""
        data = b""
        while True:
            try:
                byte = self.socket.recv(1)
                if not byte or byte == b'\n':
                    break
                data += byte
            except Exception:
                break
        return data.decode('utf-8', errors='ignore').strip()

    def _read_packet(self):
        packet = {}
        while True:
            line = self._read_line()
            if line == "":
                break
            if ':' in line:
                k, v = line.split(':', 1)
                packet[k.strip()] = v.strip()
        return packet

    def _send_raw(self, fields):
        req = ""
        for k, v in fields.items():
            req += f"{k}: {v}\r\n"
        req += "\r\n"
        self.socket.sendall(req.encode('utf-8'))

    def _start_recording(self, conf):
        filename = f"conf_{conf}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        filepath = os.path.join(CONFIG['recordings_dir'], filename)
        result = self.record_client.send_action('ConfbridgeStartRecord', {
            'Conference': conf,
            'RecordFile': filepath
        })
        if result.get('Response') == 'Success':
            print(f"[AutoRecord] 🔴 Запись конференции {conf} начата: {filename}")
        else:
            # "already being recorded" — не ошибка, просто уже пишется
            print(f"[AutoRecord] ℹ️ Старт записи {conf}: {result.get('Message', result)}")

    def _stop_recording(self, conf):
        result = self.record_client.send_action('ConfbridgeStopRecord', {'Conference': conf})
        if result.get('Response') == 'Success':
            print(f"[AutoRecord] ⏹️ Запись конференции {conf} остановлена")
        else:
            print(f"[AutoRecord] ℹ️ Стоп записи {conf}: {result.get('Message', result)}")

    def _handle_join(self, packet):
        conf = packet.get('Conference', '')
        if not conf:
            return
        num = packet.get('CallerIDNum', '')
        name = packet.get('CallerIDName') or num
        channel = packet.get('Channel', '')
        log_history(conf, 'join', num, name)

        # Модераторы заходят сразу со звуком, все остальные — заглушены
        # по умолчанию (как в Yeastar); администратор включает звук
        # вручную по просьбе через обычную кнопку "Заглушить/Включить".
        if channel and not is_moderator(num):
            result = self.record_client.send_action('ConfbridgeMute', {'Conference': conf, 'Channel': channel})
            if result.get('Response') == 'Success':
                print(f"[AutoMute] 🔇 Участник {name} ({num}) заглушен по умолчанию")
            else:
                print(f"[AutoMute] ⚠️ Не удалось заглушить {name} ({num}): {result.get('Message', result)}")

        with self.counts_lock:
            self.room_counts[conf] = self.room_counts.get(conf, 0) + 1
            count = self.room_counts[conf]
        if count == 1:
            self._start_recording(conf)

    def _handle_leave(self, packet):
        conf = packet.get('Conference', '')
        if not conf:
            return
        num = packet.get('CallerIDNum', '')
        name = packet.get('CallerIDName') or num
        log_history(conf, 'leave', num, name)

        with self.counts_lock:
            self.room_counts[conf] = max(0, self.room_counts.get(conf, 1) - 1)
            count = self.room_counts[conf]
        if count == 0:
            self._stop_recording(conf)
            self._clear_invites_for_room(conf)

    @staticmethod
    def _clear_invites_for_room(conf):
        """Конференция реально закончилась (вышел последний участник) —
        чистим её список приглашений ("не ответил" и т.п.), чтобы он не
        копился бесконечно от планёрки к планёрке. Раньше это делалось
        вручную кнопкой "Очистить список" на отдельной вкладке; теперь,
        когда приглашение переехало прямо под список конференций, чистка
        должна происходить сама — иначе старые "не ответил" от вчерашней
        встречи будут висеть под сегодняшней с тем же номером комнаты."""
        with invite_tracker_lock:
            stale_ids = [aid for aid, v in invite_tracker.items() if v.get('conference') == conf]
            for aid in stale_ids:
                del invite_tracker[aid]

    def _handle_originate_response(self, packet):
        """Реальный результат набора номера (пришёл асинхронно после
        Originate с Async: true). Response=Success значит абонент ответил
        и канал подключился; всё остальное (занято, не ответил, ошибка
        набора) — считаем неудачей и предлагаем повторный набор."""
        action_id = packet.get('ActionID', '')
        if not action_id or not action_id.startswith('invite-'):
            return
        success = packet.get('Response') == 'Success'
        with invite_tracker_lock:
            if action_id in invite_tracker:
                invite_tracker[action_id]['status'] = 'answered' if success else 'failed'

    def _loop(self):
        while self.running:
            if not self.socket:
                if not self._connect():
                    time.sleep(CONFIG['ami_retry_delay'])
                    continue
            try:
                packet = self._read_packet()
                if not packet:
                    print("[AutoRecord] ⚠️ Событийное соединение оборвалось, переподключение...")
                    self.socket = None
                    time.sleep(CONFIG['ami_retry_delay'])
                    continue
                event = packet.get('Event', '')
                if event == 'ConfbridgeJoin':
                    self._handle_join(packet)
                elif event == 'ConfbridgeLeave':
                    self._handle_leave(packet)
                elif event == 'OriginateResponse':
                    self._handle_originate_response(packet)
            except Exception as e:
                print(f"[AutoRecord] ❌ Ошибка в цикле событий: {e}")
                self.socket = None
                time.sleep(CONFIG['ami_retry_delay'])

# =============================================================================
# FLASK API
# =============================================================================

app = Flask(__name__)
app.secret_key = CONFIG['secret_key']

# Уникальное имя cookie — во избежание коллизии с другими панелями проекта
# на одном домене портала (cdr-panel/alert-panel/phone-provisioning тоже
# держат Flask-сессию; одинаковое имя "session" по умолчанию у Flask
# привело бы к тому, что посещение одной панели тихо ломает сессию другой —
# ровно так уже случалось при миграции остальных панелей).
app.config['SESSION_COOKIE_NAME'] = 'confbridge_session'

# Cookie сессии — базовый уровень защиты от CSRF. SameSite=Lax не даёт
# браузеру приложить cookie сессии к запросу, инициированному сторонним
# сайтом (классическая CSRF-атака через форму/скрипт на чужой странице),
# при этом не мешает обычной работе панели (все запросы идут с самого
# же сайта). HttpOnly — на всякий случай явно, хотя Flask и так включает
# его по умолчанию — запрещает читать cookie сессии через JS.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True


@app.after_request
def _no_cache(response):
    """Все ответы этой панели (и сам index.html, и JSON API) не должны
    кэшироваться браузером — состояние авторизации и CSRF-токен привязаны
    к текущей сессии, устаревшая закэшированная копия страницы приводит к
    невнятным ошибкам вместо явной причины (тот же урок, что и в остальных
    панелях проекта после перевода на SSO)."""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response

ami_client = None
conference_manager = None
auto_record_listener = None

def init_ami():
    global ami_client, conference_manager, auto_record_listener
    ami_client = AMIClient(CONFIG['asterisk_host'], CONFIG['asterisk_port'], CONFIG['ami_username'], CONFIG['ami_secret'])
    if not ami_client.connect():
        return False

    invite_client = AMIClient(CONFIG['asterisk_host'], CONFIG['asterisk_port'], CONFIG['ami_username'], CONFIG['ami_secret'])
    if not invite_client.connect():
        print("[AMI] ⚠️ Не удалось открыть отдельное соединение для приглашений — используется общее (возможны задержки при массовом приглашении)")
        invite_client = ami_client

    conference_manager = ConferenceManager(ami_client, invite_client)
    auto_record_listener = AutoRecordListener(
        conference_manager,
        CONFIG['asterisk_host'], CONFIG['asterisk_port'],
        CONFIG['ami_username'], CONFIG['ami_secret']
    )
    auto_record_listener.start()
    return True

# =============================================================================
# АУТЕНТИФИКАЦИЯ (SSO)
# =============================================================================
# Вход, пароли и управление учётными записями панели теперь в центральном
# sso-auth (порт 8080, единая точка для всех панелей проекта). Эта панель
# больше не проверяет пароль сама — доверяет заголовкам X-Remote-User/
# X-Remote-Role, которые nginx подставляет ПОСЛЕ проверки через auth_request.
# Доверие обосновано тем, что панель слушает только 127.0.0.1 (см.
# CONFIG['api_host']) — обратиться в обход nginx снаружи невозможно.
#
# У локальной модели прав было 4 роли (admin/manager/operator/viewer),
# у SSO — только 2 (admin/operator). Соответствие выбрано так:
#   sso 'admin'    -> локальные права роли 'admin'    (полный доступ)
#   sso 'operator' -> локальные права роли 'operator' (без kick/истории
#                      конференций управления — как было у обычного
#                      оператора и раньше)
# Роли 'manager' и 'viewer' через SSO недостижимы — если они реально
# нужны как отдельный уровень прав, потребуется расширять роли в самом
# sso-auth, а не здесь.
SSO_ROLE_MAP = {'admin': 'admin', 'operator': 'operator'}


def _trusted_remote_user():
    return request.headers.get('X-Remote-User')


@app.before_request
def load_sso_session():
    """Выполняется перед КАЖДЫМ запросом (кроме статики) — превращает
    заголовок от nginx в session['user'], как раньше это делал пароль
    через /api/login. Идемпотентно: просто переустанавливает то же самое
    на каждый запрос, лишней записи в БД/аудит при этом не происходит."""
    username = _trusted_remote_user()
    if not username:
        session.pop('user', None)
        return
    role = request.headers.get('X-Remote-Role', 'operator')
    local_role = SSO_ROLE_MAP.get(role, 'operator')
    remote_name_raw = request.headers.get('X-Remote-Name')
    full_name = unquote(remote_name_raw) if remote_name_raw else username
    session['user'] = {
        'id': None,
        'username': username,
        'role': local_role,
        'full_name': full_name,
        'permissions': ROLES.get(local_role, {}).get('permissions', []),
    }
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)


@app.before_request
def csrf_protect():
    """Защита от CSRF для всех запросов, меняющих состояние (POST/PUT/
    DELETE/PATCH), у уже залогиненных пользователей. Токен выдаётся при
    входе (session['csrf_token']), фронтенд обязан прислать его обратно
    в заголовке X-CSRF-Token — сторонний сайт, использующий чужую активную
    сессию (SameSite её уже почти не пропустит, но это второй, независимый
    рубеж защиты), этот заголовок подделать не может, так как токен ему
    неизвестен.

    Запрос без сессии (ещё не залогинен, включая сам /api/login) сюда не
    попадает — обычная 401-проверка внутри самих маршрутов справится с
    этим случаем без путаницы с CSRF."""
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return
    if 'user' not in session:
        return
    expected = session.get('csrf_token')
    provided = request.headers.get('X-CSRF-Token', '')
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        return jsonify({'error': 'Сессия устарела, обновите страницу и попробуйте снова', 'status': 'forbidden'}), 403

def require_permission(permission):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                return jsonify({'error': 'Требуется авторизация', 'status': 'unauthorized'}), 401
            if permission not in session['user'].get('permissions', []):
                return jsonify({'error': 'Недостаточно прав', 'status': 'forbidden'}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def require_admin(f):
    """Управление учётными записями панели — доступно только роли
    "Администратор" (не через общий набор permissions, а строго по роли)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': 'Требуется авторизация', 'status': 'unauthorized'}), 401
        if session['user'].get('role') != 'admin':
            return jsonify({'error': 'Доступно только администратору', 'status': 'forbidden'}), 403
        return f(*args, **kwargs)
    return decorated_function

def log_audit(user_id, username, action, target=''):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('INSERT INTO audit_log (user_id, username, action, target, ip_address) VALUES (?, ?, ?, ?, ?)',
                  (user_id, username, action, target, request.remote_addr))
    db.commit()

# =============================================================================
# МАРШРУТЫ - АУТЕНТИФИКАЦИЯ
# =============================================================================

@app.route('/')
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html')

@app.route('/api/auth/check', methods=['GET'])
def api_auth_check():
    """Раньше фронтенд опрашивал этот эндпоинт, чтобы понять, показывать
    ли форму логина. Теперь вход происходит на портале ДО того, как
    браузер вообще попадает сюда — nginx не пропустит запрос без валидной
    SSO-сессии. Поэтому единственный случай authenticated=false — это
    прямое обращение в обход nginx (не должно происходить в норме, панель
    слушает только 127.0.0.1)."""
    return jsonify({'authenticated': 'user' in session, 'user': session.get('user'), 'csrf_token': session.get('csrf_token')})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    """Локальный logout больше не разлогинивает по-настоящему — реальный
    выход происходит на портале (sso-auth). Этот эндпоинт просто чистит
    локальную Flask-сессию; фронтенд после вызова уводит пользователя на
    /logout портала (см. index.html)."""
    if 'user' in session:
        log_audit(session['user']['id'], session['user']['username'], 'logout')
    session.clear()
    return jsonify({'status': 'success'})

# =============================================================================
# УЧЁТНЫЕ ЗАПИСИ И СМЕНА ПАРОЛЯ — теперь на портале (sso-auth)
# =============================================================================
# /api/login, /api/change-password и /api/users (GET/POST/DELETE) убраны:
# единый список пользователей, роли и пароли теперь живут в sso-auth, а не
# в локальной таблице users этой панели (та остаётся в БД нетронутой, но
# больше не читается для авторизации — только исторические записи в
# audit_log продолжают ссылаться на неё по username, не по id).

# =============================================================================
# МАРШРУТЫ - КОНФЕРЕНЦИИ
# =============================================================================

@app.route('/api/conferences', methods=['GET'])
@require_permission('view')
def get_conferences():
    if not conference_manager:
        return jsonify({'error': 'AMI не подключён', 'conferences': []}), 503
    try:
        confs = conference_manager.get_all_conferences()
        return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat(), 'conferences': [
            {'conference': c.conference, 'name': c.name, 'parties': c.parties, 'recorded': c.recorded,
             'participants': [{'channel': p.channel, 'callerIDName': p.caller_id_name, 'callerIDNum': p.caller_id_num,
                                'admin': p.admin, 'muted': p.muted, 'marked': p.marked} for p in c.participants]}
            for c in confs
        ]})
    except Exception as e:
        return jsonify({'error': str(e), 'conferences': []}), 500

@app.route('/api/conference-rooms', methods=['GET'])
@require_permission('view')
def get_conference_rooms():
    """Список настроенных конференц-комнат (для вкладки "Приглашение" —
    чтобы конференция была доступна для выбора, даже если сейчас пуста).
    Управляется через веб-интерфейс, ничего не зашито в коде."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, number, name FROM conference_rooms ORDER BY number')
    rooms = [{'id': row['id'], 'conference': row['number'], 'name': row['name']} for row in cursor.fetchall()]
    return jsonify({'status': 'ok', 'rooms': rooms})

@app.route('/api/conference-rooms', methods=['POST'])
@require_permission('invite')
def create_conference_room():
    db = get_db()
    cursor = db.cursor()
    data = request.json or {}
    number = (data.get('number') or '').strip()
    name = (data.get('name') or '').strip()
    if not number or not name:
        return jsonify({'error': 'Номер и название обязательны'}), 400
    try:
        cursor.execute('INSERT INTO conference_rooms (number, name) VALUES (?, ?)', (number, name))
        db.commit()
        log_audit(session['user']['id'], session['user']['username'], 'add_conference_room', number)
        return jsonify({'status': 'success'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Такой номер конференции уже настроен'}), 400

@app.route('/api/conference-rooms/<int:room_id>', methods=['DELETE'])
@require_permission('invite')
def delete_conference_room(room_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM conference_rooms WHERE id = ?', (room_id,))
    db.commit()
    log_audit(session['user']['id'], session['user']['username'], 'remove_conference_room', str(room_id))
    return jsonify({'status': 'success'})

@app.route('/api/conferences/mute', methods=['POST'])
@require_permission('mute')
def api_mute():
    data = request.json
    if not data.get('conference') or not data.get('channel'):
        return jsonify({'error': 'Не указаны параметры'}), 400
    success = conference_manager.mute_participant(data['conference'], data['channel']) if conference_manager else False
    log_audit(session['user']['id'], session['user']['username'], 'mute', f"{data['conference']}:{data['channel']}")
    return jsonify({'status': 'success' if success else 'error'})

@app.route('/api/conferences/unmute', methods=['POST'])
@require_permission('mute')
def api_unmute():
    data = request.json
    if not data.get('conference') or not data.get('channel'):
        return jsonify({'error': 'Не указаны параметры'}), 400
    success = conference_manager.unmute_participant(data['conference'], data['channel']) if conference_manager else False
    log_audit(session['user']['id'], session['user']['username'], 'unmute', f"{data['conference']}:{data['channel']}")
    return jsonify({'status': 'success' if success else 'error'})

@app.route('/api/conferences/kick', methods=['POST'])
@require_permission('kick')
def api_kick():
    data = request.json
    if not data.get('conference') or not data.get('channel'):
        return jsonify({'error': 'Не указаны параметры'}), 400
    success = conference_manager.kick_participant(data['conference'], data['channel']) if conference_manager else False
    log_audit(session['user']['id'], session['user']['username'], 'kick', f"{data['conference']}:{data['channel']}")
    return jsonify({'status': 'success' if success else 'error'})

@app.route('/api/conferences/invite', methods=['POST'])
@require_permission('invite')
def api_invite():
    data = request.json
    if not data.get('conference') or not data.get('number'):
        return jsonify({'error': 'Не указаны параметры'}), 400
    success = conference_manager.invite_participant(data['conference'], data['number'], data.get('name')) if conference_manager else False
    log_audit(session['user']['id'], session['user']['username'], 'invite', f"{data['conference']}:{data['number']}")
    return jsonify({'status': 'success' if success else 'error'})

@app.route('/api/conferences/mass-invite', methods=['POST'])
@require_permission('invite')
def api_mass_invite():
    data = request.json
    if not data.get('conference') or not data.get('numbers'):
        return jsonify({'error': 'Не указаны параметры'}), 400
    names = data.get('names', {})
    results = conference_manager.mass_invite(data['conference'], data['numbers'], names) if conference_manager else []
    log_audit(session['user']['id'], session['user']['username'], 'mass_invite', f"{data['conference']}:{len(data['numbers'])} участников")
    return jsonify({'status': 'success', 'results': results})

@app.route('/api/invites', methods=['GET'])
@require_permission('invite')
def api_get_invites():
    """Статус набора номеров для конференции — используется вкладкой
    "Приглашение", чтобы показывать неотвеченные звонки отдельным блоком
    с возможностью повторного набора (как в Yeastar)."""
    conf = request.args.get('conference', '')
    with invite_tracker_lock:
        items = [v for v in invite_tracker.values() if not conf or v['conference'] == conf]
    items.sort(key=lambda x: x['time'], reverse=True)
    return jsonify({'status': 'ok', 'invites': items[:200]})

@app.route('/api/invites/clear', methods=['POST'])
@require_permission('invite')
def api_clear_invites():
    """Очистить список статусов набора для конференции (например, после
    завершения обзвона)."""
    data = request.json or {}
    conf = data.get('conference', '')
    with invite_tracker_lock:
        if conf:
            for action_id in [k for k, v in invite_tracker.items() if v['conference'] == conf]:
                del invite_tracker[action_id]
        else:
            invite_tracker.clear()
    return jsonify({'status': 'success'})

# =============================================================================
# МАРШРУТЫ - ЗАПИСИ (запись теперь только автоматическая, эти маршруты
# обслуживают только просмотр/скачивание/удаление уже готовых файлов)
# =============================================================================

def _guess_conference_number(filename):
    """Извлекает номер комнаты из имени файла — поддерживает оба формата:
    свой ("conf_6000_20260824...wav", через подчёркивание) и родной формат
    FreePBX при включённой опции "Record Conference: Yes" в самой FreePBX
    ("6000-6000-always-20260824-...wav", через дефис, номер комнаты
    задвоен в начале). Второй формат обнаружен 24.08.2026 — запись
    реально работала через встроенный механизм FreePBX, но панель её не
    находила по двум причинам разом: файл лежит во вложенной папке по
    дате (monitor/YYYY/MM/DD/...), а не плоско, и имя не совпадало с
    ожидаемым форматом."""
    if filename.startswith("conf_"):
        parts = filename.split("_")
        if len(parts) > 1:
            return parts[1]
    first = filename.split("-")[0]
    if first.isdigit():
        return first
    return "unknown"


@app.route('/api/recordings', methods=['GET'])
@require_permission('recordings')
def get_recordings():
    """Ищет записи РЕКУРСИВНО (Asterisk/FreePBX кладёт файлы во вложенные
    подпапки по дате: monitor/YYYY/MM/DD/..., не плоско в корень) и
    ТОЛЬКО для известных нам номеров конференц-комнат — recordings_dir
    общий с CDR-панелью (там же лежат вообще все звонки компании), без
    фильтра по номеру комнаты список был бы завален посторонними записями
    разговоров сотрудников, не имеющими отношения к конференциям.

    Каждый файл обрабатывается в своём try/except — рекурсивный обход общей с
    CDR папки может встретить файл с правами не для чтения, битым именем
    или уже удалённый параллельно другим процессом между листингом
    директории и stat(); ни одно из этого не должно ронять список
    целиком, как это едва не случилось 24.08.2026."""
    recordings = []
    base = CONFIG['recordings_dir']
    known_rooms = set(get_conference_room_names().keys())
    if os.path.exists(base) and known_rooms:
        for root, _dirs, files in os.walk(base):
            for filename in files:
                try:
                    if not (filename.endswith('.wav') or filename.endswith('.mp3')):
                        continue
                    conf = _guess_conference_number(filename)
                    if conf not in known_rooms:
                        continue
                    filepath = os.path.join(root, filename)
                    stat = os.stat(filepath)
                    recordings.append({
                        'id': hash(filepath) % 100000,
                        'conference': conf,
                        'filename': filename,
                        'size': stat.st_size,
                        'calldate': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
                except OSError:
                    # Файл исчез/недоступен между листингом и stat() —
                    # пропускаем именно его, не весь список
                    continue
    recordings.sort(key=lambda x: x['calldate'], reverse=True)
    return jsonify({'status': 'ok', 'recordings': recordings})

def _find_recording_path(recording_id):
    base = CONFIG['recordings_dir']
    if not os.path.exists(base):
        return None, None
    for root, _dirs, files in os.walk(base):
        for filename in files:
            filepath = os.path.join(root, filename)
            if str(hash(filepath) % 100000) == str(recording_id):
                return filepath, filename
    return None, None

@app.route('/api/recordings/<int:recording_id>/download', methods=['GET'])
@require_permission('recordings')
def download_recording(recording_id):
    filepath, filename = _find_recording_path(recording_id)
    if filepath:
        return send_file(filepath, as_attachment=True, download_name=filename)
    return jsonify({'error': 'Запись не найдена'}), 404

@app.route('/api/recordings/<int:recording_id>/delete', methods=['POST'])
@require_permission('recordings')
def delete_recording(recording_id):
    filepath, filename = _find_recording_path(recording_id)
    if filepath:
        os.remove(filepath)
        log_audit(session['user']['id'], session['user']['username'], 'delete_recording', filename)
        return jsonify({'status': 'success'})
    return jsonify({'error': 'Запись не найдена'}), 404

# =============================================================================
# МАРШРУТЫ - АДРЕСНАЯ КНИГА (быстрый набор во вкладке "Приглашение")
# =============================================================================

@app.route('/api/contacts', methods=['GET'])
@require_permission('invite')
def get_contacts():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM contacts ORDER BY name')
    return jsonify({'status': 'ok', 'contacts': [dict(row) for row in cursor.fetchall()]})

@app.route('/api/contacts', methods=['POST'])
@require_permission('invite')
def create_contact():
    db = get_db()
    cursor = db.cursor()
    data = request.json
    if not data.get('name') or not data.get('number'):
        return jsonify({'error': 'Имя и номер обязательны'}), 400
    cursor.execute('INSERT INTO contacts (name, number) VALUES (?, ?)', (data['name'], data['number']))
    db.commit()
    return jsonify({'status': 'success'})

@app.route('/api/contacts/<int:contact_id>', methods=['DELETE'])
@require_permission('invite')
def delete_contact(contact_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM contacts WHERE id = ?', (contact_id,))
    db.commit()
    return jsonify({'status': 'success'})

@app.route('/api/contacts/bulk', methods=['POST'])
@require_permission('invite')
def bulk_create_contacts():
    """Массовый импорт адресной книги — вставить сразу целый список
    (например, перенести группу контактов из старой панели Yeastar),
    а не добавлять по одному через форму."""
    data = request.json
    items = data.get('items', [])
    if not items:
        return jsonify({'error': 'Список пуст'}), 400
    db = get_db()
    cursor = db.cursor()
    added = 0
    for item in items:
        name = (item.get('name') or '').strip()
        number = (item.get('number') or '').strip()
        if name and number:
            cursor.execute('INSERT INTO contacts (name, number) VALUES (?, ?)', (name, number))
            added += 1
    db.commit()
    log_audit(session['user']['id'], session['user']['username'], 'bulk_import_contacts', f'{added} контактов')
    return jsonify({'status': 'success', 'added': added})

# =============================================================================
# МАРШРУТЫ - МОДЕРАТОРЫ (заходят в конференцию сразу со звуком)
# =============================================================================

@app.route('/api/moderators', methods=['GET'])
@require_permission('mute')
def get_moderators():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM moderators ORDER BY name')
    return jsonify({'status': 'ok', 'moderators': [dict(row) for row in cursor.fetchall()]})

@app.route('/api/moderators', methods=['POST'])
@require_permission('mute')
def create_moderator():
    db = get_db()
    cursor = db.cursor()
    data = request.json
    if not data.get('name') or not data.get('number'):
        return jsonify({'error': 'Имя и номер обязательны'}), 400
    try:
        cursor.execute('INSERT INTO moderators (name, number) VALUES (?, ?)', (data['name'], data['number']))
        db.commit()
        log_audit(session['user']['id'], session['user']['username'], 'add_moderator', data['number'])
        return jsonify({'status': 'success'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Этот номер уже в списке модераторов'}), 400

@app.route('/api/moderators/<int:moderator_id>', methods=['DELETE'])
@require_permission('mute')
def delete_moderator(moderator_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM moderators WHERE id = ?', (moderator_id,))
    db.commit()
    log_audit(session['user']['id'], session['user']['username'], 'remove_moderator', str(moderator_id))
    return jsonify({'status': 'success'})

@app.route('/api/extensions', methods=['GET'])
@require_permission('invite')
def api_extensions():
    """Реальный список добавочных номеров прямо из базы FreePBX — для
    выбора галочками во вкладке "Приглашение" (как в Yeastar: список
    берётся из базы АТС, а не набивается вручную)."""
    return jsonify({'status': 'ok', 'extensions': get_freepbx_extensions()})

# =============================================================================
# МАРШРУТЫ - ИСТОРИЯ ПОДКЛЮЧЕНИЙ
# =============================================================================

@app.route('/api/history', methods=['GET'])
@require_permission('history')
def get_history():
    db = get_db()
    cursor = db.cursor()
    date_filter = request.args.get('date', '')
    participant_filter = request.args.get('participant', '')
    query = "SELECT * FROM conference_history WHERE 1=1"
    params = []
    if date_filter:
        query += " AND DATE(event_time) = ?"
        params.append(date_filter)
    if participant_filter:
        query += " AND (caller_id_num LIKE ? OR caller_id_name LIKE ?)"
        like = f'%{participant_filter}%'
        params.append(like)
        params.append(like)
    query += " ORDER BY event_time DESC LIMIT 300"
    cursor.execute(query, params)
    return jsonify({'status': 'ok', 'events': [dict(row) for row in cursor.fetchall()]})

@app.route('/api/history', methods=['DELETE'])
@require_admin
def clear_history():
    """Очистка журнала истории подключений — необратимо, поэтому доступно
    только администратору (по роли, как и остальные разрушительные
    операции панели)."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM conference_history")
    db.commit()
    log_audit(session['user']['id'], session['user']['username'], 'clear_history')
    return jsonify({'status': 'success'})

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok' if ami_client and ami_client.connected else 'ami_disconnected',
        'timestamp': datetime.now().isoformat(),
        'version': '4.0'
    })

# =============================================================================
# ЗАПУСК
# =============================================================================

if __name__ == '__main__':
    print("=" * 70)
    print("   Asterisk Conference Panel API v4.0")
    print("   Автозапись + История подключений + Адресная книга")
    print("=" * 70)
    print(f"📞 AMI: {CONFIG['asterisk_host']}:{CONFIG['asterisk_port']}")
    print(f"🌐 API: {CONFIG['api_host']}:{CONFIG['api_port']}")
    print(f"💾 БД: {CONFIG['db_path']}")
    print(f"🎙️ Записи: {CONFIG['recordings_dir']}")
    print("=" * 70)

    print("\n[CHECK] Проверка ConfBridge...")
    try:
        result = subprocess.run(['asterisk', '-rx', 'module show like confbridge'], capture_output=True, text=True, timeout=5)
        if 'app_confbridge.so' in result.stdout and 'Running' in result.stdout:
            print("[CHECK] ✅ ConfBridge доступен")
        else:
            print("[CHECK] ⚠️  ConfBridge не найден или не загружен!")
    except Exception as e:
        print(f"[CHECK] ⚠️  Ошибка проверки: {e}")

    print("\n[DB] Инициализация базы данных...")
    init_db()

    print("\n[AMI] Ожидание 3 секунды перед подключением...")
    time.sleep(3)

    if not init_ami():
        print("[WARN] ⚠️  AMI не доступен. Запуск в ограниченном режиме.")
    else:
        print("[AMI] ✅ Успешное подключение к Asterisk AMI")
        print("[AutoRecord] ✅ Автозапись включена для всех конференций")

    print("\n" + "=" * 70)
    print("🚀 Запуск веб-сервера...")
    print("=" * 70)
    print(f"\n📱 Откройте в браузере: http://{CONFIG['api_host']}:{CONFIG['api_port']}/")

    password_file = os.path.join(INSTALL_DIR, 'admin_password.txt')
    if os.path.exists(password_file):
        print(f"\n🔐 Учётная запись admin:")
        with open(password_file, 'r') as f:
            print(f"   {f.read().strip()}")
        print(f"   ⚠️  Смените пароль после входа!")

    print(f"\n💡 Нажмите Ctrl+C для остановки")
    print("=" * 70 + "\n")

    try:
        app.run(host=CONFIG['api_host'], port=CONFIG['api_port'], debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n\n[INFO] 🛑 Остановка...")
        if ami_client:
            ami_client.disconnect()
