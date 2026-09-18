# Реестры победителей торгов ВИЭ

```bash
pip install -r vie_winner_registers/requirements.txt
python3 vie_winner_registers/01_download.py
python3 vie_winner_registers/02_to_csv.py
python3 vie_winner_registers/03_load_sqlite.py
```

Результат: `data/vie_winner_registers.db`. CSV хранит каждую непустую ячейку исходных PDF/XLSX вместе с координатами таблицы.

Для сканированных PDF нужен Tesseract с языками `rus` и `eng`.
