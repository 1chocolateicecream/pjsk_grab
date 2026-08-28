#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Парсинг сценариев, имён персонажей и зон."""

import config
from network import master

# ---------------------------------------------------------------------------
# кэши имён
# ---------------------------------------------------------------------------
_c2d_map = None     # кэш character2ds: id → запись
_name_map = None    # кэш: японское имя (givenName) → (characterId, русское имя)


def _looks_like_text(v):
    """похоже на читаемый текст, а не на имя ассета типа bg_a001001 / light_down_01.
    эвристика для JP: CJK-символы (U+3000+) или пробелы"""
    return any(ord(c) >= 0x3000 for c in v) or " " in v or "\u3000" in v


def _caption_from_effect(se_entry):
    """достаём из записи SpecialEffectData подпись для текста (или None, если это ассет)"""
    val = (se_entry.get("StringVal") or "").strip()
    if not val:
        return None
    t = se_entry.get("EffectType")
    if t == config.EFFECT_CHOICE:
        opts = " / ".join(o.strip() for o in val.split("/") if o.strip())
        return f"【выбор】 {opts}"
    if t == config.EFFECT_NARRATION:
        return f"〈{val}〉"                       # закадровый монолог
    if t in config.EFFECT_LOCATION:
        return f"［{val}］"                       # локация / тайм-скип
    if _looks_like_text(val):
        # на всякий: незнакомый, но текстовый телоп
        return f"［{val}］"
    return None                                   # имя ассета - мимо


def _name_lookup(window_name):
    """японское имя говорящего → (characterId, русское имя), если это ровно имя персонажа.
    «一歌» → (1, «Ичика»); «一歌の声», «？？？» → None (не подставляем)"""
    global _name_map
    if _name_map is None:
        _name_map = {}
        for c in master("gameCharacters.json"):
            ru = config.CHAR_RU.get(c["id"])
            if ru and c.get("givenName"):
                _name_map[c["givenName"]] = (c["id"], ru)
    return _name_map.get(window_name)


def _ensure_c2d_map():
    """ленивая загрузка character2ds"""
    global _c2d_map
    if _c2d_map is None:
        _c2d_map = {x["id"]: x for x in master("character2ds.json")}
    return _c2d_map


def _unit_prefix(talk_characters):
    """префикс юнита для вокалойда («L/n», «MMJ», … ; оригинал → «»). иначе «»"""
    if not talk_characters or len(talk_characters) != 1:
        return ""
    c2d = _ensure_c2d_map()
    x = c2d.get(talk_characters[0].get("Character2dId"))
    return config.UNIT_PREFIX.get(x.get("unit"), "") if x else ""


def _speaker_name(window_name, talk_characters):
    """итоговое имя говорящего для .txt. русифицируем только при точном совпадении.
    юнит-префикс вокалойду клеим ТОЛЬКО если включён --units. с --jp — имя как в игре."""
    if not config.TRANSLATE_NAMES:
        return window_name
    hit = _name_lookup(window_name)
    if not hit:
        return window_name               # «<имя>の声», «？？？», прозвища — не трогаем
    cid, ru = hit
    if config.ADD_UNITS and 21 <= cid <= 26:     # вокалойд + флаг --units → приписываем юнит
        prefix = _unit_prefix(talk_characters)
        return f"{prefix} {ru}" if prefix else ru
    return ru


# ---------------------------------------------------------------------------
# парсинг сценариев
# ---------------------------------------------------------------------------
def scenario_lines(asset_json):
    """сценарий (.asset) → [(имя, реплика), ...] в правильном порядке

    идём по Snippets (это порядок показа в игре) и склеиваем:
        Action 1 → реплика из TalkData (имя + текст);
    Action 6 → спецэффект из SpecialEffectData - лок: ［…］, выборы 【выбор】,
                    монолог 〈…〉, флешбеки 【флешбэк ↓/↑】. англ. телопы тоже ловятся
    имена переводим на русский; вокалойдам юнит клеим только с флагом --units
    """
    talk = asset_json.get("TalkData", [])
    se = asset_json.get("SpecialEffectData", [])
    out = []
    fb_open = None          # индекс маркера «флешбэк ↓» в out, пока флешбек открыт
    fb_had_text = False     # были ли реплики внутри флешбека (иначе он «пустой»)
    for sn in asset_json.get("Snippets", []):
        action, ref = sn.get("Action"), sn.get("ReferenceIndex", 0)
        if action == 1 and ref < len(talk):
            t = talk[ref]
            name = _speaker_name(
                t.get("WindowDisplayName", ""), t.get("TalkCharacters"))
            out.append((name, t.get("Body", "")))
            if fb_open is not None:
                fb_had_text = True
        elif action == 6 and ref < len(se):
            et = se[ref].get("EffectType")
            if et == 9:                  # начало флешбека
                fb_open, fb_had_text = len(out), False
                out.append((None, "【флешбэк ↓】"))
            elif et == 10:               # конец флешбека
                if fb_open is not None:
                    if fb_had_text:      # внутри был текст → ставим и закрывашку
                        out.append((None, "【конец флешбэка ↑】"))
                    else:                # пустой (просто картинка) → убираем открывашку
                        del out[fb_open]
                    fb_open = None
            else:
                cap = _caption_from_effect(se[ref])
                if cap:
                    out.append((None, cap))
    # подстраховка: если Snippets вдруг пустые - отдаём хотя бы реплики
    if not out:
        out = [(t.get("WindowDisplayName", ""), t.get("Body", ""))
                for t in talk]
    return out


def playable_lines(playable_json):
    """афтерлайв (.playable) → [(кто говорит, реплика), ...] по порядку.
    имена тоже русифицируем (юнит тут не приписать — в таймлайне нет инфы о юните)"""
    events = playable_json.get("__timelineParse", {}).get("events", [])
    talk = [e for e in events if e.get("type") == "talk" and e.get("serif")]
    talk.sort(key=lambda e: e.get("start", 0))
    out = []
    for e in talk:
        nm = e.get("character", "")
        if config.TRANSLATE_NAMES:
            hit = _name_lookup(nm)
            if hit:
                nm = hit[1]
        out.append((nm, e.get("serif", "")))
    return out


def mc_lines(asset_json):
    """старый формат афтерлайва (characterTalkEvents) → [(имя, реплика), ...]"""
    # строим маппинг Character3dId → characterId
    c3d = {x["id"]: x for x in master("character3ds.json")}

    events = asset_json.get("characterTalkEvents", [])
    events = sorted(events, key=lambda e: e.get("Time", 0))

    out = []
    for e in events:
        serif = (e.get("Serif") or "").strip()
        if not serif:
            continue
        c3d_id = e.get("Character3dId")
        char_id = (c3d.get(c3d_id) or {}).get("characterId")
        if config.TRANSLATE_NAMES and char_id and char_id in config.CHAR_RU:
            name = config.CHAR_RU[char_id]
        else:
            name = (c3d.get(c3d_id) or {}).get("name", "？？？")
        out.append((name, serif))
    return out


# ---------------------------------------------------------------------------
# хелперы имён (для cmd_talk / cmd_list)
# ---------------------------------------------------------------------------
def get_character_name(c2d_id):
    """имя персонажа по character2d ID (русское или японское, по флагу)"""
    c2d = _ensure_c2d_map()

    char_id = c2d_id
    if c2d_id in c2d:
        char_id = c2d[c2d_id].get("characterId", c2d_id)

    if not config.TRANSLATE_NAMES:
        c = next((ch for ch in master("gameCharacters.json")
                  if ch["id"] == char_id), None)
        if c and c.get("givenName"):
            return c["givenName"]
    return config.CHAR_RU.get(char_id, f"Chara {char_id}")


def get_area_name(area_id):
    """имя зоны по area ID (русское или английское, по флагу)"""
    if config.TRANSLATE_NAMES:
        return config.AREA_RU.get(area_id, f"Зона {area_id}")
    return config.AREA_EN.get(area_id, f"Zone {area_id}")
