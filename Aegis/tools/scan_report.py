import sys
import json
from pathlib import Path

# This script is in Aegis/tools. The repo root is two levels up.
# Add it to the path so this script can be run from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence.governance import SecurityScanner

# The target dir is relative to the ecosystem root, which is one level up from the repo root.
# Default to scanning Oracle, but allow specifying another repo via command line.
target_repo = sys.argv[1] if len(sys.argv) > 1 else 'Oracle'
target_path = _REPO_ROOT.parent / target_repo

if not target_path.exists() or not target_path.is_dir():
    print(f"Error: Repository '{target_repo}' not found at '{target_path}'")
    sys.exit(1)

print(f"Scanning {target_repo} for security issues...")
scanner = SecurityScanner()
report = scanner.scan_directory(str(target_path))
print(json.dumps(report, indent=2))