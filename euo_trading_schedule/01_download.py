from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from korem_document_pipeline import download_main

download_main(Path(__file__).resolve().parent, title="EUO trading schedule", list_path="/ru/news/grafik-torgov-euo", years=[2021])
