"""Constitutional alignment test."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def test_all_repositories_have_manifests():
    repos = ["Chronicle","Oracle","Nexus","Sentinel","Pulse","Atlas","Forge","Genesis","Aegis"]
    for r in repos:
        mp = ROOT / r / "repository.json"
        assert mp.exists(), f"{r} missing repository.json"
        m = json.loads(mp.read_text())
        assert m["primary_mission"], f"{r} missing mission"
        assert m["capabilities"], f"{r} missing capabilities"

if __name__ == "__main__":
    test_all_repositories_have_manifests(); print("constitutional alignment OK")
