"""Entrypoint for source runs and the portable executable."""
import sys
from pathlib import Path
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pc_bridge.app import main

if __name__ == '__main__':
    main()
