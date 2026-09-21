from pathlib import Path
import sys

def home():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]

def data_dir():
    return home() / 'AoiTalk-PC-Bridge-data'

def extension_source():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else home()
    return base / 'resources' / 'edge-browser-extension'
