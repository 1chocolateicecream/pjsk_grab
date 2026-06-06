#!/bin/bash
# двойной клик по этому файлу — и качается самый свежий ивент. всё.
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 pjsk_grab.py
else
    echo "питончика нет :( поставь его с https://www.python.org/downloads/ и попробуй снова"
fi

echo
read -n 1 -s -r -p "всё, готово! жми любую кнопку, чтоб закрыть окошко"
echo