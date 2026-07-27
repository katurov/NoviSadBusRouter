import os
import json
import logging
import redis
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_QUEUE_NAME = os.getenv("REDIS_QUEUE_NAME", "bus_routes_tasks")


_redis_client = None

def get_redis_client():
    """Возвращает клиентов подключения к Redis с пулом соединений."""
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3
            )
            _redis_client.ping()
            logger.info(f"Успешное подключение к Redis на {REDIS_HOST}:{REDIS_PORT}")
        except Exception as e:
            logger.warning(f"Не удалось подключиться к Redis ({REDIS_HOST}:{REDIS_PORT}): {e}")
            _redis_client = None
    return _redis_client

def get_cached_route_file_id(route_num: str) -> str | None:
    """Извлекает кешированный Telegram file_id карты маршрута по его номеру."""
    client = get_redis_client()
    if not client:
        return None
    try:
        key = f"route_map_file_id:{route_num.upper()}"
        return client.get(key)
    except Exception as e:
        logger.error(f"Ошибка чтения file_id из Redis для маршрута {route_num}: {e}")
        return None

def set_cached_route_file_id(route_num: str, file_id: str) -> bool:
    """Сохраняет Telegram file_id карты маршрута в Redis."""
    client = get_redis_client()
    if not client:
        return False
    try:
        key = f"route_map_file_id:{route_num.upper()}"
        client.set(key, file_id)
        logger.info(f"Сохранен file_id в Redis для маршрута {route_num}")
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения file_id в Redis для маршрута {route_num}: {e}")
        return False

def push_task(task_type: str, data: dict) -> bool:
    """Помещает сообщение/задачу в отдельную очередь Redis."""
    client = get_redis_client()
    if not client:
        return False
    try:
        payload = json.dumps({"task_type": task_type, "data": data})
        client.rpush(REDIS_QUEUE_NAME, payload)
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки задачи в очередь Redis: {e}")
        return False

def pop_task(timeout: int = 1) -> dict | None:
    """Извлекает задачу из очереди Redis."""
    client = get_redis_client()
    if not client:
        return None
    try:
        res = client.blpop(REDIS_QUEUE_NAME, timeout=timeout)
        if res:
            _, payload = res
            return json.loads(payload)
    except Exception as e:
        logger.error(f"Ошибка извлечения задачи из очереди Redis: {e}")
        return None
