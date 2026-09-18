from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from korem_document_pipeline import sqlite_main

sqlite_main(Path(__file__).resolve().parent, "vie_winner_registers.db")
