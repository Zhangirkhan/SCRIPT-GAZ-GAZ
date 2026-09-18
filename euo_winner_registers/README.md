# Реестры победителей торгов ЭУО

```bash
pip install -r euo_winner_registers/requirements.txt
python3 euo_winner_registers/01_download.py
python3 euo_winner_registers/02_to_csv.py
python3 euo_winner_registers/03_load_sqlite.py
```

Результат: `data/euo_winner_registers.db`.

Для сканированных PDF нужен Tesseract с языками `rus` и `eng`.
