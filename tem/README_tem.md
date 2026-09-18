# KOREM TEM: реестр сделок → SQLite

Пайплайн парсит PDF раздела [результаты торгов ТЭМ](https://www.korem.kz/ru/news/rezultaty-torgov-tem) из `korem_files/` (уже скачанные сканы/PDF), пишет CSV и загружает в SQLite.

PDF почти всегда — сканы: текст снимается через **Apple Vision** (`ocrmac`).

## Установка

Из корня репозитория или из этой папки:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r tem/requirements_tem.txt
```

Нужен macOS (для `ocrmac`).

## Запуск

```bash
cd tem
python 01_download_tem.py --all      # → korem_files/YYYY/MM/
python 02_pdf_to_csv_tem.py          # korem_files/ → data/csv/
python 03_load_sqlite_tem.py         # CSV → data/korem_tem.db
```

`01_download_tem.py` по умолчанию качает 1 файл; для всей коллекции — `--all`.  
`02`/`03` идемпотентны: повторный запуск пропускает уже обработанное (`--force` перезаписывает).

## Структура

```
tem/
  01_download_tem.py
  02_pdf_to_csv_tem.py
  03_load_sqlite_tem.py
  README_tem.md
  requirements_tem.txt
  korem_files/YYYY/MM/YYYY-MM-DD_{id}.pdf
  data/csv/YYYY/MM/YYYY-MM-DD_{id}.csv
  data/korem_tem.db
  data/parse_errors.jsonl
```

### SQLite

- `files` — метаданные реестра (дата торгов, период поставки, зона, итог МВт, тариф)
- `deals` — строки сделок

```sql
SELECT trade_date, organization, deal_number, volume_mw, price, amount_no_vat
FROM deals
ORDER BY trade_date, deal_number;
```
