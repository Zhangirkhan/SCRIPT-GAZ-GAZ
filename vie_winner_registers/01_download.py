from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from korem_document_pipeline import download_main

download_main(Path(__file__).resolve().parent, title="VIE winner registers", list_path="/ru/news/reestry-pobediteley-torgov-vie", years=list(range(2018, 2027)))
