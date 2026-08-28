#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pjsk_grab — удобненько сохраняем сливы секая.

Без аргументов — качает последние ивенты, свежие карты, спецухи.
Подробнее: python3 pjsk_grab.py --help
"""
import argparse
import re
import sys

# винда в консоли по дефолту не юникод, японский бы поломался к чертям. чиним
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
from processors import (
    cmd_daily, cmd_watch, cmd_event, cmd_event_solo, cmd_event_full,
    cmd_episode, cmd_card, cmd_afterlive, cmd_special, cmd_unit,
    cmd_talk, cmd_check, cmd_list,
)


# ---------------------------------------------------------------------------
# парсинг ID из аргументов CLI
# ---------------------------------------------------------------------------
def parse_single_id(val):
    val_str = str(val).strip()
    if val_str.startswith("http://") or val_str.startswith("https://"):
        m = re.search(r"eventStory/(\d+)", val_str)
        if m:
            return int(m.group(1))
        m = re.search(r"cardStory/(\d+)", val_str)
        if m:
            return int(m.group(1))
        m = re.search(r"specialStory/(\d+)", val_str)
        if m:
            return int(m.group(1))
        m = re.search(r"virtualLive/(\d+)", val_str)
        if m:
            return int(m.group(1))
        sys.exit(f"не удалось извлечь ID из ссылки: {val_str}")
    try:
        return int(val_str)
    except ValueError:
        sys.exit(f"неверный ID: {val_str}")


def parse_ids(args):
    """Разбирает список строк типа ['7-8', '10', '12,14', 'https://...']
    в упорядоченный список целых чисел."""
    ids = set()
    for arg in args:
        arg = arg.strip()
        if arg.startswith("http://") or arg.startswith("https://"):
            # Извлекаем ID из ссылок различного вида
            m = re.search(r"areaTalk/\d+/(\d+)", arg)
            if m:
                ids.add(int(m.group(1)))
                continue
            m = re.search(r"eventStory/\d+/(\d+)", arg)
            if m:
                ids.add(int(m.group(1)))
                continue
            m = re.search(r"eventStory/(\d+)", arg)
            if m:
                ids.add(int(m.group(1)))
                continue
            m = re.search(r"cardStory/(\d+)", arg)
            if m:
                ids.add(int(m.group(1)))
                continue
            m = re.search(r"specialStory/(\d+)", arg)
            if m:
                ids.add(int(m.group(1)))
                continue
            sys.exit(f"не удалось извлечь ID из ссылки: {arg}")

        # Разделяем по запятым, если есть
        parts = arg.split(",")
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                # Диапазон, например 7-8
                subparts = part.split("-")
                if len(subparts) == 2:
                    try:
                        start = int(subparts[0])
                        end = int(subparts[1])
                        # Включаем и конечную главу
                        ids.update(range(start, end + 1))
                    except ValueError:
                        sys.exit(f"неверный формат диапазона: {part}")
                else:
                    sys.exit(f"неверный формат диапазона: {part}")
            else:
                try:
                    ids.add(int(part))
                except ValueError:
                    sys.exit(f"неверный ID: {part}")
    return sorted(list(ids))


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="тырим свежий контент из Project Sekai (JP) — "
                    "без аргументов, качает самый свежий ивент")
    p.add_argument("--units", action="store_true",
                   help="приписывать вокалойдам юнит «L/n Рин» "
                        "(для ворлд линка / микс-ивентов)")
    p.add_argument("--jp", action="store_true",
                   help="оставить имена японскими, без перевода")
    p.add_argument("--app-hash", type=str, default="",
                   help="кастомный appHash для Sega CDN")
    p.add_argument("--data-hash", type=str, default="",
                   help="кастомный dataHash для Sega CDN")
    p.add_argument("--asset-host", type=str, default="",
                   help="кастомный assetbundleHostHash для Sega CDN")
    p.add_argument("--zip", type=str, default="",
                   help="ссылка или локальный путь к zip-архиву с новыми ассетами")
    sub = p.add_subparsers(dest="cmd")
    pw = sub.add_parser(
        "watch",
        help="сидеть в фоне и пинговать уведомлением, когда завезут свежак (мак)")
    pw.add_argument("minutes", type=int, nargs="?", default=1,
                    help="как часто проверять, минут (по умолчанию 1)")
    pe = sub.add_parser("event",
                        help="ивент по id [номера глав через пробел/диапазон/запятую]")
    pe.add_argument("id", type=str)
    pe.add_argument("episode_no", nargs="*", default=[],
                    help="номера глав, например: 7 8 или 7-8 или 7,8 "
                         "(без аргумента — весь ивент)")
    pep = sub.add_parser(
        "episode",
        help="главы ивента: episode <id ивента> "
             "<номера глав через пробел/диапазон/запятую>")
    pep.add_argument("event_id", type=str)
    pep.add_argument("episode_no", nargs="+",
                     help="номера глав, например: 7 8 или 7-8 или 7,8")
    pc = sub.add_parser("card", help="карта по id")
    pc.add_argument("id", type=str)
    pa = sub.add_parser("afterlive", help="афтерлайв по id virtual live")
    pa.add_argument("id", type=str)
    ps = sub.add_parser("special",
                        help="спец-история по id (там бывают выборы)")
    ps.add_argument("id", type=str)
    pu = sub.add_parser("unit",
                        help="юнит-история: unit <юнит> [номер главы]")
    pu.add_argument(
        "unit_id",
        choices=["idol", "light_sound", "street", "theme_park",
                 "school_refusal", "piapro"],
        metavar="unit_id",
        help="юнит: idol | light_sound | street | theme_park "
             "| school_refusal | piapro")
    pu.add_argument(
        "episode", type=int, nargs="?", default=None,
        help="номер эпизода (без аргумента — все эпизоды)")
    sub.add_parser(
        "check",
        help="проверить, что всё цело и ничего не проёбано")
    pt = sub.add_parser(
        "talk",
        help="диалоги: talk <ID диалога через пробел/диапазон/запятую>")
    pt.add_argument("dialogue_no", nargs="+",
                    help="номера диалогов (Area Talk), "
                         "например: 68 или 68-70 или 68,69")
    pl = sub.add_parser("list", help="найти id по названию")
    pl.add_argument(
        "kind",
        choices=["events", "afterlives", "cards", "specials", "units", "talks"])
    pl.add_argument("query", nargs="?", default="")

    a = p.parse_args()
    if a.units:
        config.ADD_UNITS = True
    if a.jp:
        config.TRANSLATE_NAMES = False
    if a.app_hash:
        config.APP_HASH = a.app_hash
    if a.data_hash:
        config.DATA_HASH = a.data_hash
    if a.asset_host:
        config.ASSET_HOST = a.asset_host
    if a.zip:
        config.ZIP_URL = a.zip
    if a.cmd is None:
        cmd_daily()
    elif a.cmd == "watch":
        cmd_watch(a.minutes)
    elif a.cmd == "event":
        eid = parse_single_id(a.id)
        if a.episode_no:
            lower_eps = [x.lower() for x in a.episode_no]
            if "full" in lower_eps:
                cmd_event_full(eid)
            elif "solo" in lower_eps:
                cmd_event_solo(eid)
            else:
                cmd_episode(eid, parse_ids(a.episode_no))
        else:
            cmd_event(eid)
    elif a.cmd == "episode":
        cmd_episode(parse_single_id(a.event_id), parse_ids(a.episode_no))
    elif a.cmd == "talk":
        cmd_talk(parse_ids(a.dialogue_no))
    elif a.cmd == "card":
        cmd_card(parse_single_id(a.id))
    elif a.cmd == "afterlive":
        cmd_afterlive(parse_single_id(a.id))
    elif a.cmd == "special":
        cmd_special(parse_single_id(a.id))
    elif a.cmd == "unit":
        cmd_unit(a.unit_id, a.episode)
    elif a.cmd == "check":
        cmd_check()
    elif a.cmd == "list":
        cmd_list(a.kind, a.query)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено, скачалось всё цельное")
        sys.exit(1)
    except Exception as e:
        print(f"\nой, что-то поломалось: {type(e).__name__}: {e}")
        sys.exit(1)