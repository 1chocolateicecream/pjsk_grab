#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Класс Run — логика сохранения файлов (тексты, картинки, афтерлайвы)."""

import datetime
import json
import os
import shutil

import config
from config import HAVE, NEW, PENDING, FAIL, _one_line
from network import fetch, exists, _atomic_write
from parsers import scenario_lines, playable_lines, mc_lines


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
    def save_afterlive(self, seg_urls, dest, header, parser_fn=None):
        """Скачивает и объединяет сегменты афтерлайва.
        parser_fn — функция парсинга (playable_lines или mc_lines).
        По умолчанию playable_lines (новый формат)."""
        if parser_fn is None:
            parser_fn = playable_lines
        if self.verify:
            on_server = bool(seg_urls) and all(exists(u) for u in seg_urls)
            return self._verify_one(dest, on_server)
        if os.path.exists(dest):
            return HAVE
        if not seg_urls:
            return PENDING
        lines = []
        try:
            first = True
            for u in seg_urls:
                data = fetch(u)
                if data is None:
                    return PENDING
                seg_lines = parser_fn(json.loads(data))
                if not first and seg_lines:
                    lines.append((None, ""))   # пустая строка между сегментами
                lines.extend(seg_lines)
                first = False
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
        prev = None        # имя предыдущего говорящего (для склейки подряд идущих реплик)
        for name, body in lines:
            name, body = (name or "").strip(), _one_line(body)
            if not name:
                # подпись/маркер (локация, флешбэк, монолог) — сбрасывает склейку
                parts.append(f"{body}\n")
                prev = None
            elif "？？？" in name:
                # загадочный голос: не склеиваем (это могут быть разные люди)
                parts.append(f"{name}: {body}\n")
                prev = None
            elif name == prev:
                # тот же говорящий подряд → имя не повторяем, только реплику
                parts.append(f"{body}\n")
            else:
                parts.append(f"{name}: {body}\n")
                prev = name
        _atomic_write(dest, "".join(parts).encode("utf-8"))

    def mirror_new(self):
        """копируем всё новое за заход в PJSK/Свежее <дата>/"""
        if not self.new_files:
            return None
        stamp = datetime.date.today().isoformat()
        out = os.path.join(config.ARCHIVE, f"Свежее {stamp}")
        for src in self.new_files:
            rel = os.path.relpath(src, config.ARCHIVE)
            dst = os.path.join(out, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        return out
