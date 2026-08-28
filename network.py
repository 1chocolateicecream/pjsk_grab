#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сетевой слой: fetch, exists, master-данные, Sega CDN."""

import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import warnings

import config

# ---------------------------------------------------------------------------
# SSL-контекст
# ---------------------------------------------------------------------------
# на питоне с python.org часто нет корневых сертификатов — берём certifi, если он
# есть, иначе непроверенный контекст (данные публичные, так что не страшно)
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl._create_unverified_context()
    print("⚠ certifi не установлен — SSL-сертификаты не проверяются "
          "(данные публичные, не страшно; pip install certifi уберёт это предупреждение)",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# диагностика сети
# ---------------------------------------------------------------------------
def _diagnose_network():
    """пробегаемся по всем доменам HEAD-запросом и печатаем понятный диагноз.
    вызывать перед любым sys.exit() по сетевой причине."""
    # (домен, за Cloudflare?, описание)
    endpoints = [
        ("raw.githubusercontent.com",         True,  "GitHub (мастер-данные)"),
        ("cdn.jsdelivr.net",                  False, "jsDelivr (резервный мастер)"),
        ("storage.sekai.best",                True,  "storage.sekai.best (зеркало ассетов)"),
        ("production-game-api.sekai.colorfulpalette.org", False, "Sega CDN (основной источник)"),
    ]
    results = []
    cf_down, cf_total = 0, 0
    print("\nдиагностика сети:", file=sys.stderr)
    for host, is_cf, label in endpoints:
        ok = _check_host(host)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label} ({host})", file=sys.stderr)
        results.append(ok)
        if is_cf:
            cf_total += 1
            if not ok:
                cf_down += 1

    all_down = not any(results)
    cf_blocked = cf_down == cf_total and cf_total > 0 and results[-1]  # sega ok, cf нет

    print(file=sys.stderr)
    if all_down:
        print("  ❌ нет сети вообще — проверь подключение к интернету", file=sys.stderr)
    elif cf_blocked:
        print("  ⚠️  похоже на блокировку Cloudflare (типично для России/Беларуси)\n"
              "     что делать: включи VPN — и запусти скрипт заново", file=sys.stderr)
    elif cf_down > 0:
        print("  ⚠️  часть сервисов недоступна — возможно, временная проблема с Cloudflare\n"
              "     что делать: подожди немного и попробуй снова, или включи VPN", file=sys.stderr)
    else:
        print("  ℹ️  все сервисы доступны — проблема скорее всего не в сети", file=sys.stderr)
    print(file=sys.stderr)


def _check_host(host, timeout=4):
    """быстрый HEAD-проб, возвращает True если хост отвечает (любой HTTP-код, даже 4xx)"""
    try:
        req = urllib.request.Request(
            f"https://{host}/", headers=config.UA, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
        return True
    except urllib.error.HTTPError:
        return True   # 403/404 — хост отвечает, значит доступен
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sega CDN (sssekai)
# ---------------------------------------------------------------------------
_sega_session = None


def get_sega_session():
    global _sega_session
    if _sega_session is not None:
        return _sega_session if _sega_session is not False else None
    try:
        import logging
        logging.getLogger("sssekai").setLevel(logging.CRITICAL + 1)
        logging.getLogger("UnityPy").setLevel(logging.CRITICAL + 1)

        # фильтруем ворнинги ТОЛЬКО от sssekai/UnityPy, а не от всего процесса
        warnings.filterwarnings("ignore", module="sssekai")
        warnings.filterwarnings("ignore", module="UnityPy")

        from sssekai.abcache import AbCache, AbCacheConfig
        req = urllib.request.Request(
            f"{config.MASTER_BASE}/versions.json", headers=config.UA)
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            v = json.loads(r.read())

        app_hash = v.get("appHash") or config.APP_HASH
        cfg = AbCacheConfig(
            app_region="jp",
            app_version=v.get("appVersion", ""),
            app_platform="android",
            app_hash=app_hash,
            asset_version=v.get("assetVersion", ""),
            asset_hash=v.get("assetHash", ""),
            asset_host=config.ASSET_HOST or None,
        )
        session = AbCache(cfg)
        try:
            session.update_signatures()
        except Exception:
            pass
        session.update_client_headers()
        session.update_abcache_index()
        _sega_session = session
        return _sega_session
    except Exception as e:
        print(f"Предупреждение: не удалось инициализировать Sega CDN сессию ({e}). "
              f"Работа продолжается через зеркало.", file=sys.stderr)
        _sega_session = False
        return None


def parse_mirror_url(url):
    """
    Разбирает URL с storage.sekai.best на:
    (bundle_name, asset_name, asset_type)
    где asset_type может быть 'MonoBehaviour' или 'Texture2D'.
    Возвращает (None, None, None), если не смогли распознать.
    """
    if "sekai-jp-assets/" not in url:
        return None, None, None

    path = url.split("sekai-jp-assets/", 1)[1]
    if "?" in path:
        path = path.split("?", 1)[0]

    parts = path.split("/")
    if len(parts) < 2:
        return None, None, None

    last_seg = parts[-1]
    name_no_ext, ext = os.path.splitext(last_seg)
    ext = ext.lower()

    if ext in (".asset", ".playable"):
        asset_type = "MonoBehaviour"
        if parts[0] == "event_story" and len(parts) >= 3 and parts[-2] == "scenario":
            bundle_name = "/".join(parts[:-2])
        else:
            bundle_name = "/".join(parts[:-1])
        return bundle_name, name_no_ext, asset_type

    elif ext == ".png":
        asset_type = "Texture2D"
        bundle_name = "/".join(parts[:-1])
        return bundle_name, name_no_ext, asset_type

    return None, None, None


def fetch_from_sega_cdn(url):
    """Качает файл из Sega CDN по его зеркальному урлу. Возвращает bytes или None."""
    bundle_name, asset_name, asset_type = parse_mirror_url(url)
    if not bundle_name:
        return None

    session = get_sega_session()
    if not session:
        return None

    entry = session.get_entry_by_bundle_name(bundle_name)
    if entry is None:
        return None

    try:
        download_url = session.get_entry_download_url(entry)
        resp = session.get(download_url)
        resp.raise_for_status()
    except Exception as e:
        print(f"Ошибка скачивания бандла {bundle_name} с Sega CDN: {e}",
              file=sys.stderr)
        return None

    try:
        from sssekai.unity.AssetBundle import load_assetbundle
        env = load_assetbundle(io.BytesIO(resp.content))
    except Exception as e:
        print(f"Ошибка декрипта/загрузки бандла {bundle_name}: {e}",
              file=sys.stderr)
        return None

    if asset_type == "MonoBehaviour":
        if url.lower().endswith(".playable"):
            try:
                events = []
                for obj in env.objects:
                    if obj.type.name == 'MonoBehaviour':
                        try:
                            data = obj.read()
                            if 'CharacterTalkClip' in getattr(data, 'm_Name', ''):
                                tree = obj.read_typetree()
                                events.append({
                                    "type": "talk",
                                    "serif": tree.get("Serif"),
                                    "character": tree.get("Character"),
                                    "start": tree.get("Offset"),
                                })
                        except Exception:
                            pass
                events.sort(key=lambda e: e.get("start", 0))
                result_dict = {"__timelineParse": {"events": events}}
                return json.dumps(result_dict, ensure_ascii=False).encode('utf-8')
            except Exception as e:
                print(f"Ошибка парсинга timeline из бандла {bundle_name}: {e}",
                      file=sys.stderr)
                return None
        else:
            for obj in env.objects:
                if obj.type.name == 'MonoBehaviour':
                    try:
                        data = obj.read()
                        if getattr(data, 'm_Name', '') == asset_name:
                            tree = obj.read_typetree()
                            return json.dumps(tree, ensure_ascii=False).encode('utf-8')
                    except Exception:
                        pass

    elif asset_type == "Texture2D":
        for obj in env.objects:
            if obj.type.name == 'Texture2D':
                try:
                    data = obj.read()
                    if getattr(data, 'm_Name', '') == asset_name or \
                       asset_name in getattr(data, 'm_Name', ''):
                        img = data.image
                        out = io.BytesIO()
                        img.save(out, format='PNG')
                        return out.getvalue()
                except Exception:
                    pass
        # фоллбэк на «первую попавшуюся текстуру» убран:
        # лучше вернуть None и показать ошибку, чем тихо отдать не ту картинку

    return None


# ---------------------------------------------------------------------------
# fetch / exists / atomic_write
# ---------------------------------------------------------------------------
def _atomic_write(dest, data):
    """пишем через временный файл + переименование: либо целый файл, либо ничего.
    это чтобы не остался обрезанный огрызок, если оборвётся на середине"""
    d = os.path.dirname(dest)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        # атомарно на той же файловой системе (win/mac/linux)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# ZIP-архив ассетов (для свежих сливов)
# ---------------------------------------------------------------------------
_zip_zf = None
_zip_entries = None
_zip_attempted = False


def fetch_from_zip(url):
    """Качает / ищет файл из zip-архива с ассетами (config.ZIP_URL или локальный файл)."""
    global _zip_zf, _zip_entries, _zip_attempted
    zip_source = getattr(config, "ZIP_URL", "")
    if not zip_source:
        return None

    if _zip_entries is None and not _zip_attempted:
        _zip_attempted = True
        try:
            import zipfile
            import io
            if zip_source.startswith("http://") or zip_source.startswith("https://"):
                req = urllib.request.Request(zip_source, headers=config.UA)
                with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r:
                    content = r.read()
                    _zip_zf = zipfile.ZipFile(io.BytesIO(content))
            elif os.path.exists(zip_source):
                _zip_zf = zipfile.ZipFile(zip_source, "r")

            if _zip_zf:
                _zip_entries = {}
                for name in _zip_zf.namelist():
                    p = name
                    for prefix in ("output/bundle/unpack/jp/", "output/bundle/unpack/"):
                        if p.startswith(prefix):
                            p = p[len(prefix):]
                            break
                    _zip_entries[p] = name
        except Exception as e:
            print(f"Предупреждение: не удалось загрузить ZIP-архив ассетов ({e})", file=sys.stderr)
            _zip_zf = None
            _zip_entries = None

    if not _zip_zf or not _zip_entries:
        return None

    if config.ASSET_BASE in url:
        rel = url.split(f"{config.ASSET_BASE}/", 1)[1]
        if rel in _zip_entries:
            return _zip_zf.read(_zip_entries[rel])
        if rel.endswith(".asset"):
            rel_json = rel[:-6] + ".json"
            if rel_json in _zip_entries:
                return _zip_zf.read(_zip_entries[rel_json])

    return None


def fetch(url, retries=3):
    """качаем байты. None — если 404 (на серваке ещё/уже нет). иначе bytes.
    если временно отвалилось (сеть, 5xx, 429) — повторяем; иначе пробрасываем ошибку"""
    req = urllib.request.Request(url, headers=config.UA)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if config.ASSET_BASE in url:
                    res = fetch_from_sega_cdn(url)
                    if res is not None:
                        return res
                    res_zip = fetch_from_zip(url)
                    if res_zip is not None:
                        return res_zip
                return None
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                if config.ASSET_BASE in url:
                    res = fetch_from_sega_cdn(url)
                    if res is not None:
                        return res
                    res_zip = fetch_from_zip(url)
                    if res_zip is not None:
                        return res_zip
                raise
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError):
            if attempt == retries - 1:
                if config.ASSET_BASE in url:
                    res = fetch_from_sega_cdn(url)
                    if res is not None:
                        return res
                    res_zip = fetch_from_zip(url)
                    if res_zip is not None:
                        return res_zip
                raise
        time.sleep(1.5 * (attempt + 1))


def exists(url):
    """по-быстрому глянуть, лежит ли оно на серваке (HEAD-запрос, без скачивания)"""
    req = urllib.request.Request(url, headers=config.UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            if r.status < 400:
                return True
    except Exception:
        pass

    if config.ASSET_BASE in url:
        try:
            bundle_name, asset_name, asset_type = parse_mirror_url(url)
            if bundle_name:
                session = get_sega_session()
                if session and session.get_entry_by_bundle_name(bundle_name) is not None:
                    return True
        except Exception:
            pass
        if fetch_from_zip(url) is not None:
            return True

    return False


# ---------------------------------------------------------------------------
# мастер-данные (с ETag-кэшированием)
# ---------------------------------------------------------------------------
# мастер-данные перепроверяем не чаще раза в MEM_TTL сек в рамках процесса, а запрос —
# условный (ETag): не изменилось → 304 (дёшево), изменилось → качаем. так watch ВСЕГДА
# видит свежий мастер (новый ивент появился ночью — увидим), а трафик остаётся крошечным.
MEM_TTL = 30
_master_cache = {}     # name -> (время, объект)
_etags = None


def _master_fetch(name, extra_headers=None):
    """качаем мастер-файл: сначала GitHub, при сетевой ошибке — jsDelivr.
    возвращает (bytes_or_none, status_code_or_none, etag_or_none).
    status=304 означает «не изменилось» (ETag совпал)."""
    headers = dict(config.UA)
    if extra_headers:
        headers.update(extra_headers)

    sources = [
        (config.MASTER_BASE,    "GitHub"),
        (config.MASTER_FALLBACK, "jsDelivr"),
    ]
    last_exc = None
    for base, label in sources:
        url = f"{base}/{name}"
        req = urllib.request.Request(url, headers=headers)
        try:
            # короткий таймаут для первого источника — быстрее переключимся на фоллбэк
            timeout = 8 if base == config.MASTER_BASE else 30
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                status = getattr(r, "status", 200)
                if status == 304:
                    return None, 304, None
                data = r.read()
                etag = r.headers.get("ETag")
                return data, status, etag
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, 304, None
            if e.code == 404:
                return None, 404, None        # файла нет — фоллбэк не поможет
            last_exc = e
            # на 5xx / 429 пробуем следующий источник
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as e:
            last_exc = e
            if base == config.MASTER_BASE:
                # сеть недоступна или таймаут — тихо переключаемся
                pass
            else:
                break   # оба упали — дальше некуда
    # оба источника упали
    if last_exc is not None:
        raise last_exc
    return None, None, None


def _etags_load():
    global _etags
    if _etags is None:
        try:
            with open(os.path.join(config.CACHE_DIR, "_etags.json")) as f:
                _etags = json.load(f)
        except Exception:
            _etags = {}
    return _etags


def _etags_save():
    try:
        with open(os.path.join(config.CACHE_DIR, "_etags.json"), "w") as f:
            json.dump(_etags, f)
    except Exception:
        pass


def _master_from_disk(name):
    try:
        with open(os.path.join(config.CACHE_DIR, name), "rb") as f:
            return json.loads(f.read())
    except Exception:
        return None


def master(name):
    """мастер-файл со свежей перепроверкой через ETag (в рамках процесса не чаще MEM_TTL).
    не изменилось → берём с диска; изменилось → качаем; сети нет → берём что на диске."""
    now = time.time()
    ent = _master_cache.get(name)
    if ent and now - ent[0] < MEM_TTL:
        return ent[1]

    os.makedirs(config.CACHE_DIR, exist_ok=True)
    path = os.path.join(config.CACHE_DIR, name)
    etags = _etags_load()
    headers = dict(config.UA)
    if name in etags and os.path.exists(path):
        headers["If-None-Match"] = etags[name]

    obj = None
    try:
        data, status, etag = _master_fetch(name, extra_headers=headers)
        if status == 304:
            obj = _master_from_disk(name)
        elif status == 404:
            sys.exit(f"не нашёлся мастер-файл {name} :(")
        elif data is not None:
            obj = json.loads(data)           # битый ответ → ValueError → диск
            _atomic_write(path, data)
            if etag:
                etags[name] = etag
                _etags_save()
    except (urllib.error.URLError, TimeoutError, ssl.SSLError,
            ConnectionError, ValueError):
        obj = _master_from_disk(name)        # нет сети / битый ответ → диск

    if obj is None:
        # подстраховка: оба источника упали и кэша нет — принудительный перезапрос
        try:
            data, _, _ = _master_fetch(name)
        except Exception:
            data = None
        if data is None:
            _diagnose_network()
            sys.exit(f"не удалось загрузить {name} — проверь интернет или попробуй с VPN")
        try:
            obj = json.loads(data)
            _atomic_write(path, data)
        except ValueError:
            sys.exit(f"мастер-файл {name} пришёл битый, попробуй ещё раз")

    _master_cache[name] = (now, obj)
    return obj
