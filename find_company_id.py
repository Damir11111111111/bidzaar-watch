#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Поиск companyId организатора на Bidzaar по части названия.

    python find_company_id.py инвитро медскан "см-клиника"

Печатает готовые строки для словаря ORGS в bidzaar_watch.py.
Смотрит последние SCAN тендеров площадки, так что находит только тех
организаторов, которые публиковались за это время (примерно последние 3 недели).
"""

import collections
import io
import json
import sys
import urllib.parse
import urllib.request

SCAN = 5000
API_URL = "https://bidzaar.com/api/process/light/procedures/available"

# в консоли Windows кодировка по умолчанию не utf-8 и русский текст ломается
for name in ("stdout", "stderr"):
    stream = getattr(sys, name)
    if stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
        setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                            errors="replace", line_buffering=True))


def main():
    needles = [a.strip().lower() for a in sys.argv[1:] if a.strip()]
    if not needles:
        sys.exit(__doc__.strip())

    params = {
        "paging.page": 1,
        "paging.size": SCAN,
        "sorting.key": "publishDate",
        "sorting.direction": "desc",
    }
    req = urllib.request.Request(
        API_URL + "?" + urllib.parse.urlencode(params, safe="[]."),
        headers={"User-Agent": "Mozilla/5.0 (compatible; bidzaar-watch/1.0)",
                 "Accept": "application/json"},
    )
    print(f"загружаю последние {SCAN} тендеров площадки…", file=sys.stderr)
    with urllib.request.urlopen(req, timeout=180) as resp:
        items = json.loads(resp.read().decode("utf-8")).get("items", [])

    print(f"просмотрено {len(items)} тендеров "
          f"(с {items[-1]['publishDate'][:10]} по {items[0]['publishDate'][:10]})\n",
          file=sys.stderr)

    counts = collections.Counter(
        (i.get("companyName") or "", i.get("companyId") or "") for i in items
    )
    for needle in needles:
        hits = [(n, cid, c) for (n, cid), c in counts.items() if needle in n.lower()]
        if not hits:
            print(f"# «{needle}» — не найдено. Часть компаний площадка пишет "
                  f"латиницей (INVITRO, OZON) — попробуйте так")
            continue
        for name, cid, count in sorted(hits, key=lambda h: -h[2]):
            print(f'    "{cid}": "{name}",  # {count} тендер(ов) за период')


if __name__ == "__main__":
    main()
