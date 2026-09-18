# KOREM EPO: результаты торгов энергопроизводящих организаций

Пайплайн скачивает PDF из раздела KOREM «Результаты торгов для ЭПО»,
извлекает таблицы в CSV и загружает их в SQLite.

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r power_producer_trading_results/requirements.txt

python power_producer_trading_results/01_download.py  # PDF → data/pdf/YYYY/MM/
python power_producer_trading_results/02_to_csv.py    # PDF → data/csv/YYYY/MM/
python power_producer_trading_results/03_load_sqlite.py # CSV → data/epo_trading_results.db
```

Все три скрипта работают с одной папкой `data/` и их можно запускать
повторно: уже скачанные и преобразованные файлы пропускаются.

## SQLite

- `files` — обработанные PDF и CSV;
- `observations` — показатели по зоне, разделу, участнику, энергоузлу и часу.
