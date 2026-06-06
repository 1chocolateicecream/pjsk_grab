#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import datetime
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request

# винда в консоли по дефолту не юникод, японский бы поломался к чертям. чиним
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# на питоне с python.org часто нет корневых сертификатов - берём certifi, если он
# есть, иначе непроверенный контекст (данные публичные, так что не страшно)
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl._create_unverified_context()

# ---------------------------------------------------------------------------
MASTER_BASE = "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-diff/main"
ASSET_BASE = "https://storage.sekai.best/sekai-jp-assets"

HERE = os.path.dirname(os.path.abspath(__file__))
# видимая «память» + сами результаты
ARCHIVE = os.path.join(HERE, "PJSK")
# кэш мастер-данных, служебное
CACHE_DIR = os.path.join(HERE, ".cache")
CACHE_TTL = 3600
UA = {"User-Agent": "pjsk-grab/2.0 (translation helper)"}

# внутренние статусы (не трогать, на них завязаны значки)
HAVE, NEW, PENDING, FAIL = "have", "new", "pending", "fail"


# ---------------------------------------------------------------------------
# сеть и мастер-данные
# ---------------------------------------------------------------------------
def fetch(url, retries=3):
    """качаем байты. None - если 404 (на серваке ещё/уже нет). иначе bytes
    если временно отвалилось (сеть, 5xx, 429) - повторяем; иначе пробрасываем ошибку"""
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (
                429,
                500,
                502,
                503,
                    504) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError):
            if attempt == retries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))


def _atomic_write(dest, data):
    """пишем через временный файл + переименование: либо целый файл, либо ничего
    это чтобы не остался обрезанный огрызок, если оборвётся на середине"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        # атомарно на той же файловой системе (win/mac/linux)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def exists(url):
    """по-быстрому глянуть, лежит ли оно на серваке (HEAD-запрос, без скачивания)"""
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            return r.status < 400
    except Exception:
        return False


_master_cache = {}


def master(name):
    """мастер-файл с кэшем на часик (в памяти + на диске). если кэш битый, тогда перекачиваем"""
    if name in _master_cache:
        return _master_cache[name]
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)

    obj = None
    if os.path.exists(path) and time.time() - \
            os.path.getmtime(path) < CACHE_TTL:
        try:
            with open(path, "rb") as f:
                obj = json.loads(f.read())
        except (ValueError, OSError):
            obj = None                   # кэш битый - забиваем и качаем заново

    if obj is None:
        data = fetch(f"{MASTER_BASE}/{name}")
        if data is None:
            sys.exit(f"не нашёлся мастер-файл {name} :(")
        try:
            obj = json.loads(data)
        except ValueError:
            sys.exit(
                f"мастер-файл {name} пришёл какой-то покоцанный, попробуй ещё раз")
        _atomic_write(path, data)        # атомарно, чтоб кэш не остался битым

    _master_cache[name] = obj
    return obj


# ---------------------------------------------------------------------------
# мелкие помощники
# ---------------------------------------------------------------------------
def safe(name):
    """делаем безопасное имя файла/папки (винда капризная на спецсимволы)."""
    name = re.sub(r'[\\/:*?"<>|]', "_", str(name))
    name = name.strip().rstrip(". ")        # винда не любит точки и пробелы в конце
    return name[:80] or "untitled"


def fmt_date(ms):
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")


def _one_line(text):
    return re.sub(r"\s*\n\s*", " ", text).strip()


# типы спецэффектов, в которых ЕСТЬ текст (проверено по данным sekai.best):
EFFECT_LOCATION = {8, 18}   # подпись локации / тайм-скип («カフェ», «数日後»)
EFFECT_CHOICE = 23          # выбор игрока: «『вариант1』/『вариант2』»
EFFECT_NARRATION = 24       # полноэкранный закадровый монолог
# остальные (7,15,16,19,22…) - это имена ассетов (фон, свет), текста там нет


def _looks_like_text(v):
    """похоже на читаемый текст, а не на имя ассета типа bg_a001001 / light_down_01"""
    return any(ord(c) >= 0x3000 for c in v) or " " in v or "　" in v


def _caption_from_effect(se_entry):
    """достаём из записи SpecialEffectData подпись для текста (или None, если это ассет)"""
    val = (se_entry.get("StringVal") or "").strip()
    if not val:
        return None
    t = se_entry.get("EffectType")
    if t == EFFECT_CHOICE:
        opts = " / ".join(o.strip() for o in val.split("/") if o.strip())
        return f"【выбор】 {opts}"
    if t == EFFECT_NARRATION:
        return f"〈{val}〉"                       # закадровый монолог
    if t in EFFECT_LOCATION:
        return f"［{val}］"                       # локация / тайм-скип
    if _looks_like_text(val):
        # на всякий: незнакомый, но текстовый телоп
        return f"［{val}］"
    return None                                   # имя ассета - мимо


def scenario_lines(asset_json):
    """сценарий (.asset) → [(имя, реплика), ...] в правильном порядке

    идём по Snippets (это порядок показа в игре) и склеиваем:
        Action 1 → реплика из TalkData (имя + текст);
    Action 6 → спецэффект из SpecialEffectData - локации ［…］, выборы 【выбор】,
                    закадровый монолог 〈…〉. английские телопы тоже ловятся
    """
    talk = asset_json.get("TalkData", [])
    se = asset_json.get("SpecialEffectData", [])
    out = []
    for sn in asset_json.get("Snippets", []):
        action, ref = sn.get("Action"), sn.get("ReferenceIndex", 0)
        if action == 1 and ref < len(talk):
            t = talk[ref]
            out.append((t.get("WindowDisplayName", ""), t.get("Body", "")))
        elif action == 6 and ref < len(se):
            cap = _caption_from_effect(se[ref])
            if cap:
                out.append((None, cap))
    # подстраховка: если Snippets вдруг пустые - отдаём хотя бы реплики
    if not out:
        out = [(t.get("WindowDisplayName", ""), t.get("Body", ""))
                for t in talk]
    return out


def playable_lines(playable_json):
    """афтерлайв (.playable) → [(кто говорит, реплика), ...] по порядку"""
    events = playable_json.get("__timelineParse", {}).get("events", [])
    talk = [e for e in events if e.get("type") == "talk" and e.get("serif")]
    talk.sort(key=lambda e: e.get("start", 0))
    return [(e.get("character", ""), e.get("serif", "")) for e in talk]


# ---------------------------------------------------------------------------
# сохранялка (пропускает уже скачанное и копит «новое за заход»)
# ---------------------------------------------------------------------------
class Run:
    """состояние одного захода: что нового накачали (для папки «Свежее»)"""

    def __init__(self, verify=False):
        self.new_files = []     # абсолютные пути всего, что скачали в этот раз
        self.verify = verify    # режим проверки: ничего не качаем, только сверяем
        self.gaps = []          # в режиме проверки: чего не хватает на диске

    def _have_on_disk(self, dest):
        return os.path.exists(dest) and os.path.getsize(dest) > 0

    def _verify_one(self, dest, on_server):
        """в режиме проверки: есть на диске → HAVE; нет, но есть на серваке → дырка"""
        if self._have_on_disk(dest):
            return HAVE
        if on_server:
            self.gaps.append(dest)
            return FAIL                      # на серваке есть, а у нас НЕТ! вот она, дырка!
        return PENDING                       # на серваке тоже нет?.. ну и ладно

    # --- текстовый сценарий ---
    def save_text(self, url, dest, header):
        if self.verify:
            return self._verify_one(dest, exists(url))
        if os.path.exists(dest):
            return HAVE
        try:
            data = fetch(url)
        except Exception:
            # если сеть отвалилась, то не роняем весь заход из-за одного файла
            return FAIL
        if data is None:
            return PENDING
        try:
            lines = scenario_lines(json.loads(data))
        except Exception:
            return FAIL
        if not lines:
            return FAIL            # разбор дал пусто?.. подозрительно, пустышку не пишем
        self._write_txt(lines, dest, header)
        self.new_files.append(dest)
        return NEW

    # --- афтерлайв: несколько кусков в один файл ---
    def save_afterlive(self, seg_urls, dest, header):
        if self.verify:
            on_server = bool(seg_urls) and all(exists(u) for u in seg_urls)
            return self._verify_one(dest, on_server)
        if os.path.exists(dest):
            return HAVE
        if not seg_urls:
            return PENDING
        lines = []
        try:
            for u in seg_urls:
                data = fetch(u)
                if data is None:
                    return PENDING           # хоть один кусок не завезли? ждём весь целиком
                lines.extend(playable_lines(json.loads(data)))
        except Exception:
            return FAIL
        if not lines:
            return FAIL
        self._write_txt(lines, dest, header)
        self.new_files.append(dest)
        return NEW

    # --- картинка / бинарь ---
    def save_binary(self, url, dest):
        if self.verify:
            return self._verify_one(dest, exists(url))
        if os.path.exists(dest):
            return HAVE
        try:
            data = fetch(url)
        except Exception:
            return FAIL
        if data is None:
            return PENDING
        _atomic_write(dest, data)
        self.new_files.append(dest)
        return NEW

    @staticmethod
    def _write_txt(lines, dest, header):
        parts = []
        if header:
            parts.append(f"# {header}\n\n")
        for name, body in lines:
            name, body = (name or "").strip(), _one_line(body)
            parts.append(f"{name}: {body}\n" if name else f"{body}\n")
        _atomic_write(dest, "".join(parts).encode("utf-8"))

    def mirror_new(self):
        """копируем всё новое за заход в PJSK/Свежее <дата>/"""
        if not self.new_files:
            return None
        stamp = datetime.date.today().isoformat()
        out = os.path.join(ARCHIVE, f"Свежее {stamp}")
        for src in self.new_files:
            rel = os.path.relpath(src, ARCHIVE)
            dst = os.path.join(out, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        return out


# значки для отчёта
MARK = {HAVE: "✓", NEW: "✓", PENDING: "·", FAIL: "✗"}


# ---------------------------------------------------------------------------
# обработка одной штуки
# ---------------------------------------------------------------------------
def process_event(ev, run):
    """качаем ивент целиком: истории, обложки, афтерлайв, карты. попутно печатаем отчётик"""
    eid, name, abn = ev["id"], ev["name"], ev["assetbundleName"]
    folder = os.path.join(ARCHIVE, "Ивенты", safe(f"{eid} - {name}"))

    print(f"\n═══ ивент {eid} · {name}")
    print(
        f"    когда: {
            fmt_date(
                ev.get('startAt'))} – {
            fmt_date(
                ev.get('closedAt'))}")

    # --- истории по эпизодам ---
    story = next((s for s in master("eventStories.json")
                    if s["eventId"] == eid), None)
    if story:
        marks, new_eps = [], []
        for ep in story["eventStoryEpisodes"]:
            url = (f"{ASSET_BASE}/event_story/{story['assetbundleName']}"
                    f"/scenario/{ep['scenarioId']}.asset")
            dest = os.path.join(
                folder, "Истории", f"{
                    safe(
                        ep['scenarioId'])}.txt")
            st = run.save_text(
                url, dest, f"{name} - эп.{
                    ep['episodeNo']} «{
                    ep['title']}»")
            marks.append(MARK[st])
            if st == NEW:
                new_eps.append(f"{ep['episodeNo']:02d}")
        on_server = sum(1 for m in marks if m == "✓")
        line = f"    истории   {
            ''.join(marks)}   {on_server}/{
            len(marks)} на серваке"
        if new_eps:
            line += f"   ← свежак: {', '.join(new_eps)}"
        print(line)

    # --- обложки ---
    logo = run.save_binary(f"{ASSET_BASE}/event/{abn}/logo/logo.png",
                            os.path.join(folder, "Картинки", "logo.png"))
    banner = run.save_binary(f"{ASSET_BASE}/home/banner/{abn}/{abn}.png",
                                os.path.join(folder, "Картинки", "banner.png"))
    print(f"    обложки   {MARK[logo]} логотип   {MARK[banner]} баннер"
            + ("   ← свежак" if NEW in (logo, banner) else ""))

    # --- афтерлайв ---
    vlid = ev.get("virtualLiveId")
    if vlid:
        vl = next(
            (v for v in master("virtualLives.json") if v["id"] == vlid),
            None)
        if vl:
            segs = sorted((s for s in vl.get("virtualLiveSetlists", [])
                            if s.get("virtualLiveSetlistType") == "mc_timeline"),
                            key=lambda s: s["seq"])
            urls = [f"{ASSET_BASE}/virtual_live/mc/timeline/{s['assetbundleName']}"
                    f"/{s['assetbundleName']}.playable" for s in segs]
            st = run.save_afterlive(
                urls, os.path.join(
                    folder, f"afterlive_{vlid}.txt"), f"{
                    vl['name']} (афтерлайв)")
            print(f"    афтерлайв {MARK[st]}" +
                    ("   ← свежак" if st == NEW else ""))

    # --- карты этого ивента ---
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
            line = _process_card(
                c, eps_by_card, run, os.path.join(
                    folder, "Карты"), indent="       ")
            if line:
                print(line)


def _process_card(c, eps_by_card, run, base_dir, indent="  "):
    """истории + картинки одной карты. возвращает строчку для отчёта
    (или None, если карты ещё нет на серваке)"""
    abn, prefix = c["assetbundleName"], c.get("prefix", "")
    folder = os.path.join(base_dir, safe(f"{abn} - {prefix}"))

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
        os.path.join(
            folder,
            "after_training.png"))
    statuses += [n, a]

    if not any(s in (HAVE, NEW) for s in statuses):
        return None                        # карты ещё нет на серваке
    # «после» бывает не у всех
    img = MARK[n] + (" " + MARK[a] if a != PENDING else "")
    fresh = "   ← свежак" if NEW in statuses else ""
    return f"{indent}{abn} · {prefix}   истории {
        ''.join(smarks) or '-'}   картинки {img}{fresh}"


DAY = 86_400_000                # сутки в миллисекундах


def process_recent_cards(run, ev):
    """свежие карты не из ивента: дни рождения и коллабы/прочее.
    их завозят тем же дропом, но в eventCards их нет. берём только те, что вышли
    в окно текущего ивента (по releaseAt), иначе притащился бы весь старый бэклог."""
    cards = master("cards.json")
    event_card_ids = {ec["cardId"] for ec in master("eventCards.json")}
    eps_by_card = {}
    for e in master("cardEpisodes.json"):
        eps_by_card.setdefault(e["cardId"], []).append(e)

    # окно = от (старт ивента − 3 дня) до конца ивента
    start = ev.get("startAt", 0) - 3 * DAY
    end = ev.get("closedAt") or (ev.get("startAt", 0) + 14 * DAY)
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
        line = _process_card(
            c, eps_by_card, run, os.path.join(
                ARCHIVE, "Карты", sub), indent="    ")
        if line:
            if not header_done:
                print("\n═══ свежие карты вне ивента (др / коллаба)")
                header_done = True
            print(line)


def process_recent_specials(run, ev):
    """спец-истории, которые выходят в окно текущего ивента (каунтдауны, юбилеи и т.п.)"""
    start = ev.get("startAt", 0) - 3 * DAY
    end = ev.get("closedAt") or (ev.get("startAt", 0) + 14 * DAY)
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
    line = _process_card(
        c, eps_by_card, run, os.path.join(
            ARCHIVE, "Карты"), indent="    ")
    print(line if line else "    (этой карты ещё нет на сервере)")


def process_afterlive(vlive_id, run):
    vl = next((v for v in master("virtualLives.json")
                if v["id"] == vlive_id), None)
    if not vl:
        sys.exit(f"не нашёлся virtual live с id={vlive_id} :(")
    segs = sorted((s for s in vl.get("virtualLiveSetlists", [])
                    if s.get("virtualLiveSetlistType") == "mc_timeline"),
                    key=lambda s: s["seq"])
    urls = [f"{ASSET_BASE}/virtual_live/mc/timeline/{s['assetbundleName']}"
            f"/{s['assetbundleName']}.playable" for s in segs]
    dest = os.path.join(ARCHIVE, "Афтерлайвы", safe(
        f"afterlive_{vlive_id} - {vl['name']}.txt"))
    st = run.save_afterlive(urls, dest, f"{vl['name']} (афтерлайв)")
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
        st = run.save_text(
            url, dest, f"{
                sp['title']} - эп.{
                ep['episodeNo']} «{
                ep['title']}»")
        marks.append(MARK[st])
        if st == NEW:
            new_eps.append(f"{ep['episodeNo']:02d}")
    on_server = sum(1 for m in marks if m == "✓")
    line = f"    эпизоды   {
        ''.join(marks)}   {on_server}/{
        len(marks)} на серваке"
    if new_eps:
        line += f"   ← свежак: {', '.join(new_eps)}"
    print(line)


# ---------------------------------------------------------------------------
# команды
# ---------------------------------------------------------------------------
def pick_current_event():
    """самый свежий ивент, у которого УЖЕ залит логотип (т.е. реально появился на сервере)"""
    events = master("events.json")
    for ev in reversed(events[-6:]):
        if exists(f"{ASSET_BASE}/event/{ev['assetbundleName']}/logo/logo.png"):
            return ev
    return events[-1]


def cmd_daily():
    print("смотрю, закинули ли сливы на сервак...")
    ev = pick_current_event()
    run = Run()
    process_event(ev, run)
    process_recent_cards(run, ev)
    process_recent_specials(run, ev)

    if run.new_files:
        out = run.mirror_new()
        print(f"\nскачено нового: {len(run.new_files)} файл(ов) !")
        print(f"архив:   {os.path.join(ARCHIVE, 'Ивенты')}")
        print(f"свежак:  {out}")
    else:
        print("\nактуалочки больше нет")
    print("\n(значки:  ✓ есть на серваке   · ещё не завезли)")


def cmd_event(event_id):
    ev = next((e for e in master("events.json") if e["id"] == event_id), None)
    if not ev:
        sys.exit(f"нет такого ивента с id={event_id} :(")
    run = Run()
    process_event(ev, run)
    out = run.mirror_new()
    print(f"\nскачено нового: {len(run.new_files)}"
            + (f";  свежак: {out}" if out else ""))


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
                    problems.append(
                        f"  ✗ почти пустой ({
                            len(body)} стр.): {rel}")
            elif fn.endswith(".png"):
                img_total += 1
                if os.path.getsize(
                        path) < 1024:    # картинка меньше 1 кб - битая
                    img_bad += 1
                    problems.append(f"  ✗ битая картинка: {rel}")

    print(f"1) целостность: есть {txt_total} текстов и {img_total} картинок")
    if problems:
        print(
            f"   нашлись косяки ({
                len(problems)}) – удали эти файлы и запусти скрипт снова:")
        for p in problems[:50]:
            print(p)
    else:
        print("   всё целое и непустое ✓")

    # --- 2) полнота: сверяемся с серваком (пути строятся ровно как при скачивании) ---
    import contextlib
    import io
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
            process_recent_cards(vr, ev)
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
    else:
        sys.exit("можно: list events | afterlives | cards | specials")
    if query:
        rows = [r for r in rows if query.lower() in r[1].lower()]
    for cid, name in rows[-200:]:
        print(f"  {cid:>7}  {name}")
    print(f"\n  показано {len(rows[-200:])} из {len(rows)}")


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="тырим свежий контент из Project Sekai (JP) "
                    "без аргументов, качает самый свежий ивент")
    sub = p.add_subparsers(dest="cmd")
    pe = sub.add_parser("event", help="ивент по id")
    pe.add_argument("id", type=int)
    pc = sub.add_parser("card", help="карта по id")
    pc.add_argument("id", type=int)
    pa = sub.add_parser("afterlive", help="афтерлайв по id virtual live")
    pa.add_argument("id", type=int)
    ps = sub.add_parser(
        "special",
        help="спец-история по id (там бывают выборы)")
    ps.add_argument("id", type=int)
    sub.add_parser(
        "check",
        help="проверить, что всё цело и ничего не проёбано")
    pl = sub.add_parser("list", help="найти id по названию")
    pl.add_argument(
        "kind",
        choices=[
            "events",
            "afterlives",
            "cards",
            "specials"])
    pl.add_argument("query", nargs="?", default="")

    a = p.parse_args()
    if a.cmd is None:
        cmd_daily()
    elif a.cmd == "event":
        cmd_event(a.id)
    elif a.cmd == "card":
        cmd_card(a.id)
    elif a.cmd == "afterlive":
        cmd_afterlive(a.id)
    elif a.cmd == "special":
        cmd_special(a.id)
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