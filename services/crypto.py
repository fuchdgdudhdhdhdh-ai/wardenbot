"""Шифрование строк Telegram-сессий."""
import os
from cryptography.fernet import Fernet, InvalidToken

class SessionCryptoError(RuntimeError):
    """Ошибка конфигурации или расшифровки сессии."""

def _fernet() -> Fernet:
    key = os.getenv("SESSION_ENC_KEY", "").strip()
    if not key:
        raise SessionCryptoError("Не задан SESSION_ENC_KEY. Создайте ключ Fernet и добавьте его в .env.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise SessionCryptoError("SESSION_ENC_KEY имеет неверный формат Fernet.") from exc

def encrypt(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SessionCryptoError("Нельзя зашифровать пустую строку сессии.")
    return _fernet().encrypt(value.encode("utf-8"))

def decrypt(value: bytes) -> str:
    if not value:
        raise SessionCryptoError("В базе отсутствует зашифрованная сессия.")
    try:
        return _fernet().decrypt(bytes(value)).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise SessionCryptoError("Не удалось расшифровать сессию. Проверьте SESSION_ENC_KEY.") from exc


def validate_key() -> None:
    """Проверить наличие и корректность ключа при запуске приложения."""
    _fernet()
