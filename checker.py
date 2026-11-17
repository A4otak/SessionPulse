import asyncio
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Tuple

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    YouBlockedUserError,
)
from telethon.tl.functions.account import GetPasswordRequest
from telethon.tl.functions.contacts import UnblockRequest

# Публичные ключи Telegram Desktop по умолчанию (если нет .json рядом с .session)
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


@dataclass
class AccountResult:
    filename: str
    status: str  # valid_clean | spamblock_temp | spamblock_perm | frozen | dead | proxy_error
    phone: str = "-"
    user_id: int = 0
    username: str = "-"
    full_name: str = "-"
    dc_id: int = 0
    premium: bool = False
    has_2fa: bool = False
    twofa_pass: str = "-"
    spambot_info: str = "-"
    proxy_used: str = "direct"

    def to_dict(self) -> dict:
        return asdict(self)


def load_session_metadata(json_path: Path) -> dict:
    if not json_path.exists():
        return {}
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_spambot_reply(text: str) -> Tuple[str, str]:
    """Определяет статус спамблока по ответу @SpamBot на RU и EN."""
    low = text.lower()

    clean_markers = (
        "no limits are currently applied",
        "good news",
        "свободен от каких-либо ограничений",
        "не применены никакие ограничения",
        "хорошие новости",
    )
    if any(m in low for m in clean_markers):
        return "valid_clean", "Без ограничений"

    frozen_markers = ("frozen", "заморожен", "удален")
    if any(m in low for m in frozen_markers):
        return "frozen", "Аккаунт заморожен"

    date_match = re.search(
        r"(\d{1,2}\s+[A-Za-zА-Яа-я]+\s+\d{4},?\s+\d{1,2}:\d{2}(?:\s*UTC)?)",
        text,
    )
    if date_match or "until" in low or "до " in low:
        until_str = date_match.group(1) if date_match else "временный"
        return "spamblock_temp", f"Временный (до {until_str})"

    perm_markers = (
        "account is limited",
        "аккаунт ограничен",
        "действия ограничены",
        "спам",
        "spam",
    )
    if any(m in low for m in perm_markers):
        return "spamblock_perm", "Вечный спамблок"

    return "unchecked", "Нестандартный ответ @SpamBot (проверить вручную)"


async def check_spambot(client: TelegramClient) -> Tuple[str, str]:
    try:
        async with client.conversation("@SpamBot", timeout=15) as conv:
            await conv.send_message("/start")
            response = await conv.get_response()
            return parse_spambot_reply(response.raw_text or "")
    except YouBlockedUserError:
        try:
            await client(UnblockRequest("@SpamBot"))
            async with client.conversation("@SpamBot", timeout=15) as conv:
                await conv.send_message("/start")
                response = await conv.get_response()
                return parse_spambot_reply(response.raw_text or "")
        except Exception as e:
            return "unchecked", f"Ошибка @SpamBot: {type(e).__name__}"
    except FloodWaitError as e:
        return "unchecked", f"FloodWait {e.seconds}s при проверке SpamBot"
    except Exception as e:
        return "unchecked", f"Нет ответа от @SpamBot ({type(e).__name__})"


async def check_single_account(
    session_path: Path,
    proxy_tuple: Optional[Tuple],
    proxy_label: str,
    check_spam: bool = True,
    timeout: int = 20,
) -> AccountResult:
    json_path = session_path.with_suffix(".json")
    meta = load_session_metadata(json_path)

    api_id = int(meta.get("app_id") or meta.get("api_id") or DEFAULT_API_ID)
    api_hash = str(meta.get("app_hash") or meta.get("api_hash") or DEFAULT_API_HASH)
    device_model = str(meta.get("device") or "Desktop")
    system_version = str(meta.get("sdk") or "Windows 10")
    app_version = str(meta.get("app_version") or "4.16.8 x64")
    lang_code = str(meta.get("lang_code") or "en")
    system_lang_code = str(meta.get("system_lang_code") or "en-US")
    twofa_from_json = str(meta.get("twoFA") or meta.get("password") or "-")

    client = TelegramClient(
        str(session_path.with_suffix("")),
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy_tuple,
        device_model=device_model,
        system_version=system_version,
        app_version=app_version,
        lang_code=lang_code,
        system_lang_code=system_lang_code,
        connection_retries=1,
        retry_delay=1,
        timeout=timeout,
    )

    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=timeout)
        except Exception as e:
            err_text = str(e).lower()
            if proxy_tuple is not None or "proxy" in err_text or "connect" in err_text or "timeout" in err_text:
                return AccountResult(
                    filename=session_path.name,
                    status="proxy_error",
                    spambot_info=f"Ошибка сети/прокси: {type(e).__name__}",
                    proxy_used=proxy_label,
                )
            return AccountResult(
                filename=session_path.name,
                status="dead",
                spambot_info=f"Ошибка подключения: {type(e).__name__}",
                proxy_used=proxy_label,
            )

        if not await client.is_user_authorized():
            return AccountResult(
                filename=session_path.name,
                status="dead",
                spambot_info="Сессия не авторизована",
                proxy_used=proxy_label,
            )

        me = await client.get_me()
        if not me:
            return AccountResult(
                filename=session_path.name,
                status="dead",
                spambot_info="Не удалось получить профиль",
                proxy_used=proxy_label,
            )

        # Проверка наличия 2FA через API
        has_2fa = twofa_from_json != "-"
        try:
            pwd_info = await client(GetPasswordRequest())
            has_2fa = bool(pwd_info.has_password)
        except Exception:
            pass

        full_name = " ".join(filter(None, [me.first_name, me.last_name])) or "-"
        username = f"@{me.username}" if me.username else "-"
        phone = f"+{me.phone}" if me.phone else "-"
        dc_id = getattr(client.session, "dc_id", 0) or 0
        premium = bool(getattr(me, "premium", False))

        if check_spam:
            status, spam_info = await check_spambot(client)
        else:
            status, spam_info = "valid_clean", "Проверка SpamBot отключена"

        return AccountResult(
            filename=session_path.name,
            status=status,
            phone=phone,
            user_id=me.id,
            username=username,
            full_name=full_name,
            dc_id=dc_id,
            premium=premium,
            has_2fa=has_2fa,
            twofa_pass=twofa_from_json,
            spambot_info=spam_info,
            proxy_used=proxy_label,
        )

    except (
        AuthKeyUnregisteredError,
        AuthKeyDuplicatedError,
        SessionExpiredError,
        SessionRevokedError,
        UserDeactivatedError,
        UserDeactivatedBanError,
    ) as e:
        return AccountResult(
            filename=session_path.name,
            status="dead",
            spambot_info=f"Мёртв ({type(e).__name__})",
            proxy_used=proxy_label,
        )
    except Exception as e:
        return AccountResult(
            filename=session_path.name,
            status="proxy_error" if proxy_tuple else "dead",
            spambot_info=f"Сбой: {type(e).__name__}",
            proxy_used=proxy_label,
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
