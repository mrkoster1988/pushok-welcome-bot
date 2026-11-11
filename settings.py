"""Проверка настроек без сети и без вывода секретов."""
import json
from pathlib import Path
from transports import numeric_id

PLATFORMS = ('telegram', 'max', 'vk')


def load_groups(path, platform):
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Не удалось прочитать groups.json: проверьте путь, права и JSON.') from None
    if not isinstance(raw, dict):
        raise ValueError('groups.json должен содержать объект.')
    if any(key in raw for key in PLATFORMS):
        groups = raw.get(platform)
    elif platform == 'telegram':
        groups = raw  # Совместимость с плоским форматом alpha.1 только для Telegram.
    else:
        raise ValueError('Для MAX и VK нужен раздел платформы в groups.json; см. groups.multi.example.json.')
    if not isinstance(groups, dict) or not groups:
        raise ValueError('Нужен непустой раздел разрешённых чатов выбранной платформы.')
    result = {}
    for chat, config in groups.items():
        if not numeric_id(chat, positive=platform == 'vk'):
            raise ValueError('Замените ID чатов на числовые значения без пробелов и ведущих нулей.')
        admins = config.get('admins') if isinstance(config, dict) else None
        if not isinstance(admins, list) or not admins or any(not numeric_id(x, positive=True) for x in admins):
            raise ValueError('В каждом чате нужен непустой admins с положительными ID пользователей.')
        result[chat] = {'admins': [str(x) for x in admins]}
    return result


def credentials(platform, env):
    token = env.get({'telegram': 'TELEGRAM_TOKEN', 'max': 'MAX_TOKEN', 'vk': 'VK_TOKEN'}[platform], '').strip()
    if not token or any(x.isspace() for x in token):
        raise ValueError('Заполните токен выбранной платформы в окружении; пробелы недопустимы.')
    if platform == 'vk' and not numeric_id(env.get('VK_GROUP_ID'), positive=True):
        raise ValueError('VK_GROUP_ID должен содержать положительный ID сообщества без префикса club.')
    ca_file = env.get('MAX_CA_FILE')
    if platform == 'max' and ca_file and not Path(ca_file).is_file():
        raise ValueError('MAX_CA_FILE должен указывать на существующий PEM-файл доверенного сертификата.')
    return token
