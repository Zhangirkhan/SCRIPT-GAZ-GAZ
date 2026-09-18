# KOREM: результаты торгов майнеров → SQLite

Пайплайн скачивает PDF с [korem.kz](https://www.korem.kz/ru/news/rezultaty-torgov-tsm), парсит таблицы в CSV и загружает в SQLite.

Для раздела **ТЭМ** (реестр сделок) см. папку [`tem/`](tem/README_tem.md).

Годы: **2023–2026**. Раздел: централизованные торги для цифровых майнеров.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
python 01_download.py          # PDF → data/pdf/YYYY/MM/
python 02_pdf_to_csv.py        # PDF → data/csv/YYYY/MM/
python 03_load_sqlite.py       # CSV → data/korem.db
```

Все скрипты идемпотентны: повторный запуск пропускает уже обработанные файлы.

## Структура данных

```
data/
  pdf/YYYY/MM/YYYY-MM-DD_{id}.pdf
  csv/YYYY/MM/YYYY-MM-DD_{id}.csv
  manifest.jsonl
  parse_errors.jsonl
  korem.db
```

### SQLite

- `files` — каталог скачанных PDF/CSV
- `observations` — ряды по зонам/метрикам/часам (1–24, `0` = Итого)

Пример запроса:

```sql
SELECT trade_date, zone, hour, value
FROM observations
WHERE metric = 'quota_so' AND zone = 'north_south'
ORDER BY trade_date, hour;
```
