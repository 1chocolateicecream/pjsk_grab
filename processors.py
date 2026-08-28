#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обработка контента (ивенты, карты, афтерлайвы, спецухи) и команды."""

import contextlib
import datetime
import io
import json
import os
import re
import subprocess
import sys
import time

import config
from config import (
    ARCHIVE, ASSET_BASE, HAVE, NEW, PENDING, FAIL, MARK,
    CHAR_FULL_RU, CHAR_RU, CHAR_SHORT_NAME, UNIT_RU,
    DAY, SCAN_EVENTS,
    safe, fmt_date,
)
from network import fetch, exists, master, _atomic_write
from parsers import mc_lines, get_character_name, get_area_name
from run import Run


# ---------------------------------------------------------------------------
# обработка ивента
# ---------------------------------------------------------------------------
def process_event(ev, run, no_cards=False):
    """качаем ивент целиком: истории, обложки, афтерлайв, карты. попутно печатаем отчётик"""
    eid, name, abn = ev["id"], ev["name"], ev["assetbundleName"]
    folder = os.path.join(ARCHIVE, "Ивенты", safe(f"{eid} - {name}"))

    start = fmt_date(ev.get("startAt"))
    end = fmt_date(ev.get("closedAt"))
    print(f"\n═══ ивент {eid} · {name}")
    print(f"    когда: {start} – {end}")

    story = next((s for s in master("eventStories.json")
                  if s["eventId"] == eid), None)
    if story:
        marks, new_eps = [], []
        for ep in story["eventStoryEpisodes"]:
            url = (f"{ASSET_BASE}/event_story/{story['assetbundleName']}"
                   f"/scenario/{ep['scenarioId']}.asset")
            dest = os.path.join(folder, "Истории", f"{safe(ep['scenarioId'])}.txt")
            header = f"{name} - эп.{ep['episodeNo']} «{ep['title']}»"
            st = run.save_text(url, dest, header)
            marks.append(MARK[st])
            if st == NEW:
                new_eps.append(f"{ep['episodeNo']:02d}")
        on_server = sum(1 for m in marks if m == "✓")
        line = f"    истории   {''.join(marks)}   {on_server}/{len(marks)} на серваке"
        if new_eps:
            line += f"   ← свежак: {', '.join(new_eps)}"
        print(line)

    # --- обложки ---
    logo = run.save_binary(f"{ASSET_BASE}/event/{abn}/logo/logo.png",
                           os.path.join(folder, "Картинки", "logo.png"))
    banner = run.save_binary(f"{ASSET_BASE}/home/banner/{abn}/{abn}.png",
                             os.path.join(folder, "Картинки", "banner.png"))
    cover_line = f"    обложки   {MARK[logo]} логотип   {MARK[banner]} баннер"
    if NEW in (logo, banner):
        cover_line += "   ← свежак"
    print(cover_line)

    # --- афтерлайв ---
    vlid = ev.get("virtualLiveId")
    if vlid:
        vl = next((v for v in master("virtualLives.json") if v["id"] == vlid), None)
        if vl:
            segs = sorted(
                (s for s in vl.get("virtualLiveSetlists", [])
                 if s.get("virtualLiveSetlistType") == "mc_timeline"),
                key=lambda s: s["seq"])
            urls = [f"{ASSET_BASE}/virtual_live/mc/timeline/{s['assetbundleName']}"
                    f"/{s['assetbundleName']}.playable" for s in segs]
            dest = os.path.join(folder, f"afterlive_{vlid}.txt")
            st = run.save_afterlive(urls, dest, f"{vl['name']} (афтерлайв)")
            fresh = "   ← свежак" if st == NEW else ""
            print(f"    афтерлайв {MARK[st]}{fresh}")

    # --- карты этого ивента ---
    if not no_cards:
        card_ids = [ec["cardId"]
                    for ec in master("eventCards.json") if ec.get("eventId") == eid]
        if card_ids:
            cards = {c["id"]: c for c in master("cards.json")}
            eps_by_card = {}
            for e in master("cardEpisodes.json"):
                eps_by_card.setdefault(e["cardId"], []).append(e)
            print(f"    карты ({len(card_ids)}):")
            for cid in card_ids:
                c = cards.get(cid)
                if not c:
                    continue
                line = _process_card(c, eps_by_card, run,
                                     os.path.join(folder, "Карты"), indent="       ")
                if line:
                    print(line)


def _process_card(c, eps_by_card, run, base_dir, indent="  "):
    """истории + картинки одной карты. возвращает строчку для отчёта
    (или None, если карты ещё нет на серваке)"""
    abn, prefix = c["assetbundleName"], c.get("prefix", "")
    who = CHAR_FULL_RU.get(c.get("characterId")) or abn   # понятное имя вместо res...
    folder = os.path.join(base_dir, safe(f"{who} - {prefix}"))

    statuses, smarks = [], []
    for ep in sorted(eps_by_card.get(c["id"], []), key=lambda e: e["seq"]):
        url = f"{ASSET_BASE}/character/member/{abn}/{ep['scenarioId']}.asset"
        dest = os.path.join(folder, f"{safe(ep['scenarioId'])}.txt")
        st = run.save_text(url, dest, f"{prefix} - {ep['title']}")
        statuses.append(st)
        smarks.append(MARK[st])

    n = run.save_binary(f"{ASSET_BASE}/character/member/{abn}/card_normal.png",
                        os.path.join(folder, "normal.png"))
    a = run.save_binary(
        f"{ASSET_BASE}/character/member/{abn}/card_after_training.png",
        os.path.join(folder, "after_training.png"))
    statuses += [n, a]

    if not any(s in (HAVE, NEW) for s in statuses):
        return None                        # карты ещё нет на серваке
    # «после» бывает не у всех
    img = MARK[n] + (" " + MARK[a] if a != PENDING else "")
    fresh = "   ← свежак" if NEW in statuses else ""
    stories = "".join(smarks) or "-"
    return f"{indent}{who} · {prefix}   истории {stories}   картинки {img}{fresh}"


def process_recent_cards(run):
    """свежие карты не из ивента: дни рождения и коллабы/прочее.
    берём те, что вышли недавно (окно по releaseAt вокруг «сейчас»): старый бэклог
    не тащим, но ловим всё из текущего дропа и datamine ближайшего будущего."""
    cards = master("cards.json")
    event_card_ids = {ec["cardId"] for ec in master("eventCards.json")}
    eps_by_card = {}
    for e in master("cardEpisodes.json"):
        eps_by_card.setdefault(e["cardId"], []).append(e)

    now = time.time() * 1000          # окно вокруг «сейчас», а не вокруг ивента
    start, end = now - 12 * DAY, now + 30 * DAY
    recent = [c for c in cards
              if c["id"] not in event_card_ids
              and c.get("releaseAt")
              and start <= c["releaseAt"] <= end]
    if not recent:
        return

    header_done = False
    for c in recent:
        bday = c.get("cardRarityType") == "rarity_birthday"
        sub = "Дни рождения" if bday else "Коллабы и прочее"
        line = _process_card(c, eps_by_card, run,
                             os.path.join(ARCHIVE, "Карты", sub), indent="    ")
        if line:
            if not header_done:
                print("\n═══ свежие карты вне ивента (др / коллаба)")
                header_done = True
            print(line)


def probe_birthday_cdns(run):
    """CDN-пробинг ДР-карт: когда база ещё не обновилась, угадываем следующий
    assetbundleName по паттерну и проверяем CDN напрямую HEAD-запросом.
    Это позволяет поймать карту сразу при заливке на сервер — не ждать гит-коммита.
    Запускается параллельно с обычным process_recent_cards."""

    # Дни рождения в ближайшие BDAY_WINDOW дней
    BDAY_WINDOW = 14         # дней до ДР включительно (карту заливают заранее)
    BDAY_LOOKBACK = 3        # дней после ДР (вдруг залили чуть позже)
    NO_PROBE_RANGE = range(5, 18)   # пробуем next_no + 5..+17 от последнего известного

    try:
        profiles = master("characterProfiles.json")
    except Exception:
        return

    # Собираем id персонажей с ДР в окне
    now_dt = datetime.datetime.now()
    upcoming_char_ids = set()
    for p in profiles:
        bd = p.get("birthday", "")   # формат: «6月24日»
        m_match = re.search(r"(\d+)月(\d+)日", bd)
        if not m_match:
            continue
        bmon, bday_num = int(m_match.group(1)), int(m_match.group(2))
        # Создаём дату в текущем году (или следующем, если ДР уже прошёл)
        for yr in (now_dt.year, now_dt.year + 1):
            try:
                bdate = now_dt.replace(year=yr, month=bmon, day=bday_num,
                                       hour=0, minute=0, second=0, microsecond=0)
            except ValueError:
                continue
            delta = (bdate - now_dt).days
            if -BDAY_LOOKBACK <= delta <= BDAY_WINDOW:
                upcoming_char_ids.add(p["characterId"])
                break

    if not upcoming_char_ids:
        return

    # Для каждого персонажа с ближайшим ДР берём его последнюю известную ДР-карту
    try:
        cards = master("cards.json")
    except Exception:
        return

    bday_by_char = {}
    for c in cards:
        if c.get("cardRarityType") == "rarity_birthday":
            cid = c.get("characterId")
            if cid in upcoming_char_ids:
                bday_by_char.setdefault(cid, []).append(c)

    # Уже известные abn чтобы не дублировать то, что уже есть в базе
    known_abns = {c["assetbundleName"] for c in cards if c.get("assetbundleName")}

    header_done = False
    for cid in upcoming_char_ids:
        char_bday_cards = sorted(bday_by_char.get(cid, []),
                                 key=lambda c: c["id"])
        if not char_bday_cards:
            continue
        last = char_bday_cards[-1]
        last_abn = last.get("assetbundleName", "")
        m = re.match(r"(res\d+)_no(\d+)", last_abn)
        if not m:
            continue
        prefix_abn, last_no = m.group(1), int(m.group(2))

        who = CHAR_FULL_RU.get(cid) or f"char{cid}"

        # Пробуем next_no в диапазоне: до 15 выше последнего известного
        for delta_no in NO_PROBE_RANGE:
            candidate_no = last_no + delta_no
            abn = f"{prefix_abn}_no{candidate_no:03d}"
            if abn in known_abns:
                continue    # уже в базе — обработается через process_recent_cards

            probe_url = f"{ASSET_BASE}/character/member/{abn}/card_normal.png"
            if not exists(probe_url):
                continue

            # Нашли! Скачиваем обложку + after_training (если есть) + пробуем эпизоды
            folder = os.path.join(ARCHIVE, "Карты", "Дни рождения",
                                  safe(f"{who} - (ДР-карта, abn={abn})"))
            normal_st = run.save_binary(probe_url, os.path.join(folder, "normal.png"))
            after_url = f"{ASSET_BASE}/character/member/{abn}/card_after_training.png"
            after_st = run.save_binary(after_url, os.path.join(folder, "after_training.png"))

            # Эпизоды: паттерн стабильный — «{cid:03d}{no:03d}_{shortname}{ep_idx}.asset»
            # например: 016054_rui01.asset, 016054_rui02.asset
            short = CHAR_SHORT_NAME.get(cid, f"char{cid}")
            char_str = f"{cid:03d}{candidate_no:03d}"
            ep_statuses = []
            for ep_idx in ("01", "02"):
                ep_name = f"{char_str}_{short}{ep_idx}"
                ep_url = f"{ASSET_BASE}/character/member/{abn}/{ep_name}.asset"
                if exists(ep_url):
                    dest = os.path.join(folder, f"{safe(ep_name)}.txt")
                    st = run.save_text(ep_url, dest, f"{who} ДР - эп.{ep_idx}")
                    ep_statuses.append(st)

            all_st = [normal_st, after_st] + ep_statuses
            if not any(s in (HAVE, NEW) for s in all_st):
                continue

            img = MARK[normal_st] + (" " + MARK[after_st] if after_st != PENDING else "")
            eps_mark = "".join(MARK[s] for s in ep_statuses) or "·"
            fresh = "   ← свежак" if NEW in all_st else ""
            if not header_done:
                print("\n═══ ДР-карты (CDN-пробинг, база ещё не обновилась)")
                header_done = True
            print(f"    {who} · abn={abn}   истории {eps_mark}   картинки {img}{fresh}")
            break   # нашли — следующие delta_no не нужны


def process_recent_specials(run):
    """спец-истории, выходящие недавно (каунтдауны, юбилеи) — окно вокруг «сейчас»."""
    now = time.time() * 1000
    start, end = now - 12 * DAY, now + 30 * DAY
    specials = [s for s in master("specialStories.json")
                if s.get("startAt") and start <= s["startAt"] <= end]
    for sp in specials:
        # тихо скипаем, если на серваке ещё пусто (эпизодов нет)
        eps = sp.get("episodes", [])
        if not eps:
            continue
        process_special(sp["id"], run)


def process_card(card_id, run):
    c = next((c for c in master("cards.json") if c["id"] == card_id), None)
    if not c:
        sys.exit(f"нет такой карты с id={card_id} :(")
    eps_by_card = {}
    for e in master("cardEpisodes.json"):
        eps_by_card.setdefault(e["cardId"], []).append(e)
    print(f"\n═══ карта {card_id} · {c.get('prefix', '')}")
    line = _process_card(c, eps_by_card, run,
                         os.path.join(ARCHIVE, "Карты"), indent="    ")
    print(line if line else "    (этой карты ещё нет на сервере)")


def process_afterlive(vlive_id, run):
    vl = next((v for v in master("virtualLives.json")
               if v["id"] == vlive_id), None)
    if not vl:
        sys.exit(f"не нашёлся virtual live с id={vlive_id} :(")

    setlists = vl.get("virtualLiveSetlists", [])

    # новый формат: mc_timeline → .playable
    new_segs = sorted((s for s in setlists
                       if s.get("virtualLiveSetlistType") == "mc_timeline"),
                      key=lambda s: s["seq"])
    # старый формат: mc → .asset (сценарий)
    old_segs = sorted((s for s in setlists
                       if s.get("virtualLiveSetlistType") == "mc"),
                      key=lambda s: s["seq"])

    dest = os.path.join(ARCHIVE, "Афтерлайвы",
                        safe(f"afterlive_{vlive_id} - {vl['name']}.txt"))

    if new_segs:
        urls = [f"{ASSET_BASE}/virtual_live/mc/timeline/{s['assetbundleName']}"
                f"/{s['assetbundleName']}.playable" for s in new_segs]
        st = run.save_afterlive(urls, dest, f"{vl['name']} (афтерлайв)")
    elif old_segs:
        urls = [f"{ASSET_BASE}/virtual_live/mc/scenario/{s['assetbundleName']}"
                f"/{s['assetbundleName']}.asset" for s in old_segs]
        st = run.save_afterlive(urls, dest, f"{vl['name']} (афтерлайв)",
                                parser_fn=mc_lines)
    else:
        st = PENDING

    print(f"\n═══ афтерлайв {vlive_id} · {vl['name']}   {MARK[st]}")


def process_special(special_id, run):
    """спец-история целиком (опенинги, каунтдауны и пр.). вот тут как раз бывают ВЫБОРЫ."""
    sp = next((s for s in master("specialStories.json")
               if s["id"] == special_id), None)
    if not sp:
        sys.exit(f"нет такой спец-истории с id={special_id} :(")
    folder = os.path.join(ARCHIVE, "Спец-истории",
                          safe(f"{special_id} - {sp['title']}"))
    print(f"\n═══ спец-история {special_id} · {sp['title']}")

    marks, new_eps = [], []
    for ep in sp.get("episodes", []):
        # путь тут плавающий: папка то по abn эпизода, то по abn истории -
        # пробуем оба
        cands = [
            f"{ASSET_BASE}/scenario/special/{ep['assetbundleName']}/{ep['scenarioId']}.asset",
            f"{ASSET_BASE}/scenario/special/{sp['assetbundleName']}/{ep['scenarioId']}.asset",
        ]
        url = next((u for u in cands if exists(u)), cands[0])
        dest = os.path.join(folder, f"{safe(ep['scenarioId'])}.txt")
        header = f"{sp['title']} - эп.{ep['episodeNo']} «{ep['title']}»"
        st = run.save_text(url, dest, header)
        marks.append(MARK[st])
        if st == NEW:
            new_eps.append(f"{ep['episodeNo']:02d}")
    on_server = sum(1 for m in marks if m == "✓")
    line = f"    эпизоды   {''.join(marks)}   {on_server}/{len(marks)} на серваке"
    if new_eps:
        line += f"   ← свежак: {', '.join(new_eps)}"
    print(line)


def process_unit_story(unit_id, chapter_no, run):
    all_units = master("unitStories.json")
    unit_data = next((u for u in all_units if u["unit"] == unit_id), None)
    if not unit_data:
        known = ", ".join(u["unit"] for u in all_units)
        sys.exit(f"нет юнита «{unit_id}» :( есть: {known}")

    unit_name = UNIT_RU.get(unit_id, unit_id)

    # собираем все эпизоды из всех глав в плоский список
    all_episodes = []
    for ch in unit_data["chapters"]:
        for ep in ch["episodes"]:
            all_episodes.append((ch, ep))

    if chapter_no is not None:
        filtered = [(ch, ep) for ch, ep in all_episodes
                    if ep["episodeNo"] == chapter_no]
        if not filtered:
            have = ", ".join(str(ep["episodeNo"]) for _, ep in all_episodes)
            sys.exit(f"у юнита {unit_id} нет эпизода {chapter_no} (есть: {have})")
        pairs = filtered
    else:
        pairs = all_episodes

    label = f" · эп. {chapter_no}" if chapter_no is not None else ""
    print(f"\n═══ юнит-история · {unit_name}{label}")

    for ch, ep in pairs:
        folder = os.path.join(ARCHIVE, "Юнит-истории", safe(unit_name),
                              safe(f"Глава {ch['chapterNo']} - {ch['title']}"))
        url = (f"{ASSET_BASE}/scenario/unitstory"
               f"/{ch['assetbundleName']}/{ep['scenarioId']}.asset")
        dest = os.path.join(folder, f"{safe(ep['scenarioId'])}.txt")
        header = f"{ch['title']} · {ep['episodeNoLabel']} «{ep['title']}»"
        st = run.save_text(url, dest, header)
        fresh = "  ← свежак" if st == NEW else ""
        print(f"  эп.{ep['episodeNo']:>2} {ep['episodeNoLabel']:<10} "
              f"«{ep['title']}»  {MARK[st]}{fresh}")


# ---------------------------------------------------------------------------
# уведомления (мак)
# ---------------------------------------------------------------------------
def notify(title, message):
    """пингуем громко: звук + тост + звоночек терминала.
    мак: afplay + osascript. винда: winsound + PowerShell Toast + SetThreadExecutionState.
    на прочих платформах — только звоночек терминала (лучше чем ничего)."""

    # звоночек терминала — работает везде, иконка мигнёт в таскбаре/доке
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass

    if sys.platform == "darwin":
        _notify_mac(title, message)
    elif sys.platform == "win32":
        _notify_windows(title, message)


def _notify_mac(title, message):
    # 1) звук напрямую — работает даже если баннеры запрещены/«не беспокоить»
    try:
        subprocess.Popen(["afplay", "/System/Library/Sounds/Hero.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    # 2) баннер в центре уведомлений (если разрешён для терминала)
    try:
        subprocess.run(
            ["osascript", "-e",
             f"display notification {json.dumps(message)} "
             f"with title {json.dumps(title)}"],
            check=False, capture_output=True)
    except Exception:
        pass


def _notify_windows(title, message):
    # 1) звук — winsound встроен, не нужно ничего ставить
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass

    # 2) Toast через PowerShell (Win 10/11, без доп. пакетов).
    #    Строим XML руками — надёжнее XPath (SelectSingleNode вернёт $null если узел
    #    не найдётся и PowerShell крашнется).
    #    AUMID = путь к powershell.exe — гарантированно зарегистрирован в системе;
    #    произвольная строка типа "pjsk-grab" не зарегистрирована и Toast молча
    #    игнорируется на части сборок Win10.
    try:
        def _xml_esc(s):
            """XML-экранирование: &, <, >, кавычки — и убираем всё что сломает PS"""
            return (s
                    .replace("&",  "&amp;")
                    .replace("<",  "&lt;")
                    .replace(">",  "&gt;")
                    .replace('"',  "&quot;")
                    .replace("'",  "&apos;"))

        xml = (
            "<toast><visual><binding template='ToastText02'>"
            f"<text id='1'>{_xml_esc(title)}</text>"
            f"<text id='2'>{_xml_esc(message)}</text>"
            "</binding></visual></toast>"
        )
        # AUMID стандартного PowerShell — есть на любой Win10/11
        aumid = (r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
                 r"\WindowsPowerShell\v1.0\powershell.exe")
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager,"
            " Windows.UI.Notifications, ContentType=WindowsRuntime]|Out-Null; "
            "$xd=[Windows.Data.Xml.Dom.XmlDocument,"
            " Windows.Data.Xml.Dom, ContentType=WindowsRuntime]::new(); "
            f"$xd.LoadXml('{xml}'); "
            "$tn=[Windows.UI.Notifications.ToastNotification]::new($xd); "
            "[Windows.UI.Notifications.ToastNotificationManager]"
            f"::CreateToastNotifier('{aumid}').Show($tn)"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            check=False, capture_output=True, timeout=6,
        )
    except Exception:
        # тихо падаем — звук уже сыграл, уведомление не критично
        pass

# ---------------------------------------------------------------------------
# команды
# ---------------------------------------------------------------------------
def recent_events(n=SCAN_EVENTS):
    """последние n ивентов по id (новейшие). НЕ гадаем «один текущий» и НЕ зависим от
    логотипа — берём свежак целиком, а skip-if-exists не даёт качать уже скачанное.
    так мы не пропустим дроп, даже если логотип ещё не залит или вышло сразу два ивента."""
    events = sorted(master("events.json"), key=lambda e: e.get("id", 0))
    return events[-n:] if events else []


def cmd_daily():
    print("смотрю, закинули ли сливы на сервак...")
    run = Run()
    for ev in recent_events():
        process_event(ev, run)
    process_recent_cards(run)
    probe_birthday_cdns(run)
    process_recent_specials(run)

    if run.new_files:
        out = run.mirror_new()
        print(f"\nскачено нового: {len(run.new_files)} файл(ов) !")
        print(f"архив:   {os.path.join(ARCHIVE, 'Ивенты')}")
        print(f"свежак:  {out}")
    else:
        print("\nактуалочки больше нет")
    print("\n(значки:  ✓ есть на серваке   · ещё не завезли)")


def cmd_watch(minutes):
    """сидим в фоне и ждём свежак: как завезут — качаем и пингуем уведомлением.
    первый проход подтягивает то, что пропустили, и тоже орет об этом, чтобы не пропустить
    сливы во время оффлайна. Ctrl-C чтоб выйти."""
    # не даём компу уснуть во время watch
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("⚓️ caffeinate активен: Mac не уснет во время работы watch.", flush=True)
        except Exception as e:
            print(f"⚠️ не удалось запустить caffeinate: {e}", flush=True)
    elif sys.platform == "win32":
        try:
            import ctypes
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED — не спать, пока процесс жив
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
            print("⚓️ SetThreadExecutionState активен: Windows не уснет во время работы watch.",
                  flush=True)
        except Exception as e:
            print(f"⚠️ не удалось включить keep-awake: {e}", flush=True)

    print(f"слежу за серваком, проверяю каждые {minutes} мин. Ctrl-C чтоб остановить.",
          flush=True)
    print("(можно свернуть окно — как завезут свежак, прилетит уведомление)\n",
          flush=True)
    first = True
    while True:
        ts = datetime.datetime.now().strftime("%H:%M")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):     # отчёт ловим в буфер, не спамим
                run = Run()
                for ev in recent_events():
                    process_event(ev, run)
                process_recent_cards(run)
                probe_birthday_cdns(run)
                process_recent_specials(run)
            n = len(run.new_files)
            if n:
                out = run.mirror_new()
                if first:
                    title = "pjsk_grab — синхронизация! 🔔"
                else:
                    title = "pjsk_grab — свежак завезли! 🔔"
                msg = f"скачано {n} файл(ов), лежит в PJSK"

                kind = "СИНХРОНИЗАЦИЯ" if first else "СВЕЖАК"
                print(f"[{ts}] 🔔 {kind}! скачано {n} файл(ов):", flush=True)
                print(buf.getvalue().rstrip(), flush=True)
                print(f"свежак:  {out}\n", flush=True)
                notify(title, msg)
            else:
                if first:
                    print(f"[{ts}] всё уже на месте, слежу за свежаком…", flush=True)
                else:
                    print(f"[{ts}] нового нет, жду дальше…", flush=True)
            first = False
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{ts}] заминка: {type(e).__name__}: {e} (попробую в след. раз)",
                  flush=True)
        time.sleep(minutes * 60)


def cmd_event(event_id):
    ev = next((e for e in master("events.json") if e["id"] == event_id), None)
    if not ev:
        sys.exit(f"нет такого ивента с id={event_id} :(")
    run = Run()
    process_event(ev, run)
    out = run.mirror_new()
    print(f"\nскачено нового: {len(run.new_files)}"
          + (f";  свежак: {out}" if out else ""))


def cmd_event_solo(event_id):
    """скачиваем ивент целиком (истории, обложки, афтерлайв), но без карт"""
    ev = next((e for e in master("events.json") if e["id"] == event_id), None)
    if not ev:
        sys.exit(f"нет такого ивента с id={event_id} :(")
    run = Run()
    process_event(ev, run, no_cards=True)
    out = run.mirror_new()
    print(f"\nскачено нового: {len(run.new_files)}"
          + (f";  свежак: {out}" if out else ""))


def cmd_event_full(event_id):
    """скачиваем только истории ивента по id и объединяем их в один файл"""
    ev = next((e for e in master("events.json") if e["id"] == event_id), None)
    if not ev:
        sys.exit(f"нет такого ивента с id={event_id} :(")

    eid, name = ev["id"], ev["name"]
    folder = os.path.join(ARCHIVE, "Ивенты", safe(f"{eid} - {name}"))

    print(f"\n═══ ивент {eid} · {name} (сборка одной книгой)")

    story = next((s for s in master("eventStories.json")
                  if s["eventId"] == eid), None)
    if not story:
        sys.exit(f"нет историй для ивента с id={eid} :(")

    run = Run()
    marks, new_eps = [], []
    any_new = False
    for ep in story["eventStoryEpisodes"]:
        url = (f"{ASSET_BASE}/event_story/{story['assetbundleName']}"
               f"/scenario/{ep['scenarioId']}.asset")
        dest = os.path.join(folder, "Истории", f"{safe(ep['scenarioId'])}.txt")
        header = f"{name} - эп.{ep['episodeNo']} «{ep['title']}»"
        st = run.save_text(url, dest, header)
        marks.append(MARK[st])
        if st == NEW:
            new_eps.append(f"{ep['episodeNo']:02d}")
            any_new = True

    on_server = sum(1 for m in marks if m == "✓")
    line = f"    истории   {''.join(marks)}   {on_server}/{len(marks)} на серваке"
    if new_eps:
        line += f"   ← свежак: {', '.join(new_eps)}"
    print(line)

    # Собираем все главы в один файл
    combined_dest = os.path.join(folder, f"{safe(name)}.txt")
    combined_parts = []
    all_ok = True
    for ep in story["eventStoryEpisodes"]:
        ep_file = os.path.join(folder, "Истории", f"{safe(ep['scenarioId'])}.txt")
        if os.path.exists(ep_file):
            try:
                with open(ep_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    if combined_parts:
                        combined_parts.append("\n\n\n")
                    combined_parts.append(content.strip())
            except Exception:
                all_ok = False
        else:
            all_ok = False

    if all_ok and combined_parts:
        is_new = any_new or not os.path.exists(combined_dest)
        try:
            _atomic_write(combined_dest, "".join(combined_parts).encode("utf-8"))
            if is_new:
                run.new_files.append(combined_dest)
            print(f"    все главы объединены в один файл: {safe(name)}.txt")
        except Exception as e:
            print(f"    не удалось объединить главы: {e}")
    else:
        print("    не все главы еще доступны на сервере, не могу объединить")

    out = run.mirror_new()
    if out:
        print(f"\nскачено нового: {len(run.new_files)};  свежак: {out}")
    else:
        print("\nуже лежит в архиве или не все главы доступны")


def cmd_unit(unit_id, chapter_no):
    run = Run()
    process_unit_story(unit_id, chapter_no, run)
    out = run.mirror_new()
    print(f"\nскачено нового: {len(run.new_files)}"
          + (f";  свежак: {out}" if out else ""))


def cmd_episode(event_id, episode_nos):
    """указанные главы ивента, и ничего больше (ни картинок, ни остального)"""
    if isinstance(episode_nos, int):
        episode_nos = [episode_nos]

    ev = next((e for e in master("events.json") if e["id"] == event_id), None)
    if not ev:
        sys.exit(f"нет такого ивента с id={event_id} :(")
    story = next((s for s in master("eventStories.json")
                  if s["eventId"] == event_id), None)
    if not story:
        sys.exit(f"у ивента {event_id} вообще нет истории :(")

    print(f"\n═══ ивент {event_id} · {ev['name']}")

    run = Run()
    any_downloaded = False
    has_new = False

    for episode_no in episode_nos:
        ep = next((e for e in story["eventStoryEpisodes"]
                   if e["episodeNo"] == episode_no), None)
        if not ep:
            have = ", ".join(str(e["episodeNo"])
                             for e in story["eventStoryEpisodes"])
            print(f"    глава {episode_no}   ✗ (нет такой главы в ивенте, есть: {have})")
            continue

        folder = os.path.join(ARCHIVE, "Ивенты", safe(f"{event_id} - {ev['name']}"))
        dest = os.path.join(folder, "Истории", f"{safe(ep['scenarioId'])}.txt")
        url = (f"{ASSET_BASE}/event_story/{story['assetbundleName']}"
               f"/scenario/{ep['scenarioId']}.asset")
        header = f"{ev['name']} - эп.{ep['episodeNo']} «{ep['title']}»"
        st = run.save_text(url, dest, header)

        print(f"    глава {ep['episodeNo']} «{ep['title']}»   {MARK[st]}")
        if st == NEW:
            has_new = True
            any_downloaded = True
        elif st == HAVE:
            any_downloaded = True

    out = run.mirror_new()
    if has_new:
        print(f"\nскачено нового: {len(run.new_files)}"
              + (f";  свежак: {out}" if out else ""))
    elif any_downloaded:
        print("\nуже лежит в архиве (заново не качаю)")
    else:
        print("\nне получилось скачать или главы не найдены")


def cmd_talk(dialogue_ids):
    """скачиваем указанные диалоги (Area Talk) по ID"""
    if isinstance(dialogue_ids, int):
        dialogue_ids = [dialogue_ids]

    data = master("actionSets.json")
    if not data:
        sys.exit("не удалось загрузить базу диалогов actionSets.json :(")

    print("\n═══ диалоги (Area Talk)")

    run = Run()
    any_downloaded = False
    has_new = False

    for d_id in dialogue_ids:
        entry = next((e for e in data if e["id"] == d_id), None)
        if not entry:
            sys.exit(f"нет такого диалога с ID={d_id} :(")

        scenario_id = entry.get("scenarioId")
        if not scenario_id:
            print(f"    диалог {d_id:<5}   ✗ (нет текста, только анимация персонажей)")
            continue

        speaker_names = [get_character_name(cid)
                         for cid in entry.get("characterIds", [])]

        area_id = entry.get("areaId", 0)
        area_name = get_area_name(area_id)

        folder = os.path.join(ARCHIVE, "Диалоги", safe(area_name))
        if speaker_names:
            filename = f"{d_id} - {', '.join(speaker_names)}"
        else:
            filename = f"{d_id} - {scenario_id}"
        dest = os.path.join(folder, f"{safe(filename)}.txt")

        group_dir = f"group{d_id // 100}"
        url = f"{ASSET_BASE}/scenario/actionset/{group_dir}/{scenario_id}.asset"

        speakers = ", ".join(speaker_names) if speaker_names else scenario_id
        header = f"Диалог {d_id} · {area_name} ({speakers})"
        st = run.save_text(url, dest, header)

        print(f"    диалог {d_id:<5} ({area_name})   {MARK[st]}")
        if st == NEW:
            has_new = True
            any_downloaded = True
        elif st == HAVE:
            any_downloaded = True

    out = run.mirror_new()
    if has_new:
        print(f"\nскачено нового: {len(run.new_files)}"
              + (f";  свежак: {out}" if out else ""))
    elif any_downloaded:
        print("\nуже лежит в архиве (заново не качаю)")
    else:
        print("\nне получилось скачать или диалоги не найдены")


def cmd_card(card_id):
    run = Run()
    process_card(card_id, run)
    print(f"\nскачено нового: {len(run.new_files)}")


def cmd_afterlive(vlive_id):
    run = Run()
    process_afterlive(vlive_id, run)
    print(f"\nскачено нового: {len(run.new_files)}")


def cmd_special(special_id):
    run = Run()
    process_special(special_id, run)
    out = run.mirror_new()
    print(f"\nскачено нового: {len(run.new_files)}"
          + (f";  свежак: {out}" if out else ""))


def cmd_check():
    """самопроверка: проходимся по архиву и ищем пустые/битые файлы + сверяем с серваком,
    не проёбано ли чего. ничего не качает, просто смотрит"""
    if not os.path.isdir(ARCHIVE):
        print("папки PJSK ещё нет, сначала запусти скрипт без всего")
        return

    txt_total = txt_bad = img_total = img_bad = 0
    problems = []
    for root, _dirs, files in os.walk(ARCHIVE):
        if os.path.basename(root).startswith("Свежее"):
            continue                      # копии для чата не проверяем
        for fn in files:
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, ARCHIVE)
            if fn.endswith(".txt"):
                txt_total += 1
                try:
                    with open(path, encoding="utf-8") as f:
                        body = [
                            ln for ln in f if ln.strip() and not ln.startswith("#")]
                except Exception as e:
                    txt_bad += 1
                    problems.append(f"  ✗ не читается: {rel}  ({e})")
                    continue
                if len(body) < 2:         # история без реплик - что-то не то
                    txt_bad += 1
                    problems.append(f"  ✗ почти пустой ({len(body)} стр.): {rel}")
            elif fn.endswith(".png"):
                img_total += 1
                if os.path.getsize(path) < 1024:    # картинка меньше 1 кб - битая
                    img_bad += 1
                    problems.append(f"  ✗ битая картинка: {rel}")

    print(f"1) целостность: есть {txt_total} текстов и {img_total} картинок")
    if problems:
        print(f"   нашлись косяки ({len(problems)}) – "
              f"удали эти файлы и запусти скрипт снова:")
        for p in problems[:50]:
            print(p)
    else:
        print("   всё целое и непустое ✓")

    # --- 2) полнота: сверяемся с серваком (пути строятся ровно как при скачивании) ---
    events = []
    evdir = os.path.join(ARCHIVE, "Ивенты")
    if os.path.isdir(evdir):
        for d in sorted(os.listdir(evdir)):
            m = re.match(r"(\d+)\s*-", d)
            if m:
                events.append(int(m.group(1)))

    print("\n2) сверяю архив с серваком...")
    gaps = []
    for eid in events:
        ev = next((e for e in master("events.json") if e["id"] == eid), None)
        if not ev:
            continue
        vr = Run(verify=True)
        # тихо, нужен только итог
        with contextlib.redirect_stdout(io.StringIO()):
            process_event(ev, vr)
            process_recent_cards(vr)
        gaps += vr.gaps
    if gaps:
        print(f"   ОЙ... на серваке есть, а у тебя нет ({len(gaps)} шт). "
              f"запусти обычный прогон, чтоб докачать:")
        for g in gaps[:50]:
            print(f"   ↓ {os.path.relpath(g, ARCHIVE)}")
    else:
        print("   всё, что сейчас на серваке, лежит у тебя ✓")

    if events:
        print("\n3) можешь сверить главы глазами на секай бесте:")
        for eid in events:
            print(f"   ивент {eid}:  https://sekai.best/event/{eid}")


def cmd_list(kind, query):
    if kind == "events":
        rows = [(e["id"], e["name"]) for e in master("events.json")]
    elif kind in ("afterlives", "afterlife"):
        rows = [(v["id"], v["name"]) for v in master("virtualLives.json")]
    elif kind == "cards":
        rows = [(c["id"], c.get("prefix", "")) for c in master("cards.json")]
    elif kind in ("specials", "special"):
        rows = [(s["id"], s["title"]) for s in master("specialStories.json")]
    elif kind in ("units", "unit"):
        for u in master("unitStories.json"):
            nch = len(u["chapters"])
            nep = sum(len(ch["episodes"]) for ch in u["chapters"])
            name = UNIT_RU.get(u["unit"], u["unit"])
            print(f"  {u['unit']:<20}  {name}  ({nch} гл., {nep} эп.)")
        return
    elif kind in ("talks", "talk", "dialogue", "dialogues"):
        rows = []
        for e in master("actionSets.json"):
            if not e.get("scenarioId"):
                continue
            char_names = [get_character_name(cid) for cid in e.get("characterIds", [])]
            area_name = get_area_name(e.get("areaId", 0))
            desc = f"({area_name}) {', '.join(char_names)}"
            rows.append((e["id"], desc))
    else:
        sys.exit("можно: list events | afterlives | cards | specials | units | talks")
    if query:
        rows = [r for r in rows if query.lower() in r[1].lower()]
    for cid, name in rows[-200:]:
        print(f"  {cid:>7}  {name}")
    print(f"\n  показано {len(rows[-200:])} из {len(rows)}")
