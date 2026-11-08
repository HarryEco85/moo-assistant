"""Double-click (or use the desktop shortcut) to start Moo without a console window."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from moo.app import main

if __name__ == "__main__":
    main()
