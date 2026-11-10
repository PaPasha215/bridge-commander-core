"""Build dist/bridge-commander.pyz: one file that runs anywhere with Python 3.9+ (`python3 bridge-commander.pyz`)."""
import os
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
target = os.path.join(ROOT, "dist", "bridge-commander.pyz")
os.makedirs(os.path.dirname(target), exist_ok=True)
zipapp.create_archive(os.path.join(ROOT, "src"), target, interpreter="/usr/bin/env python3",
                      main="bridge_commander.cli:main", filter=lambda p: "__pycache__" not in p.parts)
print(target)
