# KOREM EPO: результаты торгов энергопроизводящих организаций

Пайплайн скачивает PDF из раздела KOREM «Результаты торгов для ЭПО»,
извлекает таблицы в CSV и загружает их в SQLite.

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r epo/requirements.txt

python epo/01_download.py       # PDF → epo/data/pdf/YYYY/MM/
python epo/02_to_csv.py         # PDF → epo/data/csv/YYYY/MM/
python epo/03_load_sqlite.py    # CSV → epo/data/epo_trading_results.db
```

Все три скрипта работают с одной папкой `epo/data/` и их можно запускать
повторно: уже скачанные и преобразованные файлы пропускаются.

## SQLite

- `files` — обработанные PDF и CSV;
- `observations` — показатели по зоне, разделу, участнику, энергоузлу и часу.
