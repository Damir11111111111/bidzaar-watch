#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мониторинг новых тендеров на bidzaar.com и отправка уведомлений в Telegram.

Отслеживает публичный фид закупок (авторизация не требуется), фильтрует по
списку организаторов и по ключевым словам, и пишет в Telegram про каждый
новый тендер один раз.

Запуск:
    TG_BOT_TOKEN=... TG_CHAT_ID=... python bidzaar_watch.py

Полезные переменные окружения:
    TG_BOT_TOKEN   токен бота (обязательно)
    TG_CHAT_ID     id получателя (обязательно)
    STATE_FILE     путь к файлу состояния (по умолчанию state/seen.json)
    DRY_RUN=1      ничего не отправлять, только напечатать в консоль
    RESEED=1       перезаписать состояние текущим фидом без отправки сообщений
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

# Организаторы. Ключ — companyId на Bidzaar (стабильный), значение — как
# показывать в сообщении. companyId надёжнее названия: название организатор
# может поменять в любой момент, id — нет.
ORGS = {
    "019ac421-8d56-78f3-87c1-242145599e38": "МКПАО «МД Медикал Груп Инвестментс»",
    "5f32216f-a2ad-4e36-b7ea-66fab6b92d55": "АО «Европейский Медицинский Центр»",
    "64dfa8bc-48e9-45d7-acdd-27390af78fcd": "ООО «Группа компаний СМ-Клиника»",
    "c814b787-a771-4fed-be37-49484d5b2597": "АО «Медскан»",
    "de0e84e7-e364-4f2b-a008-e005ea36e6cf": "Группа компаний «Медси»",
    "9b3c4695-cdaa-436e-a0bf-8406fedd387a": "INVITRO (Инвитро)",
}

# Ключевые слова. Ищем по названию тендера, регистр не важен.
# Подпись в кортеже — то, что попадёт в сообщение как причина совпадения.
KEYWORD_RULES = [
    # «проект» покрывает: проектирование, проектная/проектно-, проектной
    # документации, проект. Отсекаем «проектор» — это техника, не ПИР.
    (r"проект(?!ор)", "проект / проектирование / проектная документация"),
    # ПИР только как отдельное слово, чтобы не ловить «спирт», «пирог», «папирус».
    (r"(?<![0-9А-Яа-яЁёA-Za-z])пир(?:ы|ов|ам)?(?![0-9А-Яа-яЁёA-Za-z])", "ПИР"),
    # «рабочая документация» во всех падежах.
    (r"рабоч\w*[\s\-]+документац", "рабочая документация"),
]

# False — присылать вообще все тендеры этих организаторов, без фильтра по словам.
REQUIRE_KEYWORD = True

# Тендеры старше этого возраста не считаются новыми, даже если их нет в
# состоянии. Страховка от того, чтобы при потере state-файла в чат не улетела
# вся история.
MAX_AGE_DAYS = 21

API_URL = "https://bidzaar.com/api/process/light/procedures/available"
TENDER_URL = "https://bidzaar.com/app/process/light/{id}"
PAGE_SIZE = 100
SEEN_CAP = 5000

STATUS_NAMES = {
    0: "Черновик",
    1: "Приём предложений",
    2: "Подведение итогов",
    3: "Завершён",
    8: "Отложенная публикация",
}

MSK = timezone(timedelta(hours=3))
KEYWORD_RES = [(re.compile(p, re.IGNORECASE), label) for p, label in KEYWORD_RULES]


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now(MSK):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def http_json(url, data=None, tries=4):
    """GET/POST с ретраями. Возвращает разобранный JSON."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; bidzaar-watch/1.0)",
                    "Accept": "application/json",
                    **({"Content-Type": "application/json"} if data else {}),
                },
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # сеть, 5xx, таймаут — пробуем ещё
            last = exc
            if attempt < tries:
                wait = 2 ** attempt
                log(f"запрос не удался ({exc}); повтор через {wait} с")
                time.sleep(wait)
    raise last


def msk(iso):
    """'2026-08-17T16:13:05.830957Z' -> '17.08.2026 19:13 МСК'"""
    if not iso:
        return None
    try:
        txt = iso.replace("Z", "+00:00")
        # у Bidzaar микросекунды бывают 6-7 знаков — fromisoformat строгий
        txt = re.sub(r"\.(\d{6})\d*", r".\1", txt)
        return f"{datetime.fromisoformat(txt).astimezone(MSK):%d.%m.%Y %H:%M} МСК"
    except ValueError:
        return iso


def age_days(iso):
    try:
        txt = re.sub(r"\.(\d{6})\d*", r".\1", (iso or "").replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - datetime.fromisoformat(txt)).days
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# Состояние
# --------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        return set(state.get("seen", [])), bool(state.get("seeded"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), False


def save_state(path, seen, order):
    """Храним ids в порядке убывания даты публикации и обрезаем хвост.

    Если список не изменился — файл не трогаем. Это важно для GitHub Actions:
    иначе каждый запуск давал бы коммит, то есть ~3000 коммитов в месяц.
    """
    ranked = [i for i in order if i in seen]
    ranked += sorted(seen - set(ranked))
    ranked = ranked[:SEEN_CAP]

    previous, was_seeded = load_state(path)
    if was_seeded and previous == set(ranked):
        return

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"seeded": True, "updated": datetime.now(MSK).isoformat(),
                   "seen": ranked}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Bidzaar
# --------------------------------------------------------------------------

def fetch_tenders():
    """Свежие тендеры выбранных организаторов. Фильтр по компаниям — на сервере."""
    params = {
        "paging.page": 1,
        "paging.size": PAGE_SIZE,
        "sorting.key": "publishDate",
        "sorting.direction": "desc",
        "logic": "and",
        "filters[0].operator": "in",
        "filters[0].field": "companyId",
        "filters[0].value": "[" + ",".join(ORGS) + "]",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params, safe="[].")
    data = http_json(url)
    return data.get("items", [])


def match_keywords(name):
    """Список подписей сработавших правил. Пустой список = не подходит."""
    return [label for rx, label in KEYWORD_RES if rx.search(name or "")]


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def build_message(item, labels):
    org = ORGS.get(item.get("companyId")) or item.get("companyName") or "—"
    e = html.escape
    lines = [
        "🔔 <b>Новый тендер на Bidzaar</b>",
        "",
        f"🏢 <b>{e(org)}</b>",
        f"📄 {e(item.get('name') or '—')}",
    ]
    if item.get("number"):
        lines.append(f"№ <code>{e(str(item['number']))}</code>")
    if labels:
        lines.append(f"🔖 Совпадение: {e(', '.join(labels))}")

    lines.append("")
    published = msk(item.get("publishDate"))
    if published:
        lines.append(f"📅 Опубликован: {e(published)}")
    deadline = msk(item.get("acceptanceEndDate") or item.get("finishDate"))
    if deadline:
        lines.append(f"⏳ Приём предложений до: {e(deadline)}")
    status = STATUS_NAMES.get(item.get("status"))
    if status:
        lines.append(f"📊 Статус: {e(status)}")

    cities = []
    for addr in item.get("deliveryAddresses") or []:
        city = addr.get("city") or addr.get("region")
        if city and city not in cities:
            cities.append(city)
    if cities:
        lines.append(f"📍 {e(', '.join(cities[:3]))}")

    lines.append("")
    lines.append(TENDER_URL.format(id=item.get("id")))
    return "\n".join(lines)


def send_telegram(token, chat_id, text):
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode("utf-8")
    result = http_json(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram отказал: {result}")


# --------------------------------------------------------------------------

def main():
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    state_file = os.environ.get("STATE_FILE", os.path.join("state", "seen.json"))
    dry_run = os.environ.get("DRY_RUN") == "1"
    reseed = os.environ.get("RESEED") == "1"

    if not dry_run and not (token and chat_id):
        sys.exit("Нужны переменные окружения TG_BOT_TOKEN и TG_CHAT_ID")

    seen, seeded = load_state(state_file)
    items = fetch_tenders()
    log(f"получено тендеров этих организаторов: {len(items)}")

    order = [i["id"] for i in items if i.get("id")]

    # Первый запуск (или RESEED): запоминаем текущий фид молча, иначе в чат
    # улетит вся история сразу.
    if reseed or not seeded:
        save_state(state_file, seen | set(order), order)
        log(f"состояние инициализировано: {len(seen | set(order))} тендеров, сообщения не отправлялись")
        return

    fresh = []
    for item in items:
        tid = item.get("id")
        if not tid or tid in seen:
            continue
        if age_days(item.get("publishDate")) > MAX_AGE_DAYS:
            seen.add(tid)          # старьё: помечаем, но не шумим
            continue
        labels = match_keywords(item.get("name"))
        if REQUIRE_KEYWORD and not labels:
            seen.add(tid)          # организатор наш, тематика — нет
            continue
        fresh.append((item, labels))

    fresh.reverse()                # от старых к новым — так читается в чате
    log(f"подходящих новых: {len(fresh)}")

    sent = 0
    for item, labels in fresh:
        text = build_message(item, labels)
        if dry_run:
            print("\n--- сообщение ---\n" + text)
        else:
            try:
                send_telegram(token, chat_id, text)
            except Exception as exc:
                # не помечаем как отправленное — попробуем на следующем запуске
                log(f"не удалось отправить {item.get('number')}: {exc}")
                continue
            time.sleep(1)          # уважаем лимиты Telegram
        seen.add(item["id"])
        sent += 1

    if not dry_run:
        save_state(state_file, seen, order)
    log(f"отправлено сообщений: {sent}")


if __name__ == "__main__":
    main()
