"""``python -m src.web [--port 8765] [--open|--no-open]``"""
import sys

from src.web.app import main

sys.exit(main())
