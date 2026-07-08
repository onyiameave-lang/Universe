"""
Genesis.core.sandbox
===================
Sandboxed validation: a newborn agent must PROVE it runs before it joins the
ecosystem. (Book III Part II Ch XV Certification; Book IV Part II Ch XI Testing;
Book I Part IV Article II birth process stage: simulation/validation.)

An institutional factory never deploys unproven code. This harness materializes
the synthesized agent into an ISOLATED temporary directory, imports it in a
subprocess (so a crash cannot take down Genesis), runs its generated unit tests,
and instantiates the agent to confirm it starts, exposes capabilities, and
handles an unknown task gracefully. Only agents that pass certification proceed.

Everything runs in a temp dir that is cleaned up; nothing touches the live tree
until Aegis + human approval in the factory's deploy step.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class Sandbox:
    def __init__(self, eco_root: Path):
        self.eco_root = eco_root

    def certify(self, files: Dict[str, str], module_name: str,
                class_name: str) -> Dict[str, Any]:
        """
        Write files to a temp repo, import + instantiate the agent + run its
        tests in a subprocess. Returns a certification report.
        """
        report = {"certified": False, "checks": {}, "stage_failed": None}
        with tempfile.TemporaryDirectory(prefix="genesis_sandbox_") as tmp:
            tmpdir = Path(tmp)
            repo = tmpdir / "Candidate"
            # materialize URS skeleton + synthesized files
            for d in ("agents", "testing", "memory", "constitutional", "core"):
                (repo / d).mkdir(parents=True, exist_ok=True)
                (repo / d / "__init__.py").write_text("", encoding="utf-8")
            for rel, content in files.items():
                target = repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            # Stage 1: import + instantiate + basic behavior, in a subprocess.
            probe = self._probe_script(module_name, class_name)
            probe_path = tmpdir / "_probe.py"
            probe_path.write_text(probe, encoding="utf-8")
            r1 = self._run([sys.executable, str(probe_path)],
                          cwd=repo, env_paths=[str(self.eco_root), str(repo)])
            report["checks"]["import_and_run"] = r1
            if not r1["ok"]:
                report["stage_failed"] = "import_and_run"
                return report

            # Stage 2: run the generated unit tests.
            test_file = next((rel for rel in files if rel.startswith("testing/test_")), None)
            if test_file:
                r2 = self._run([sys.executable, "-m", "pytest", "-q", test_file],
                              cwd=repo, env_paths=[str(self.eco_root), str(repo)])
                # pytest may be absent; fall back to unittest
                if r2.get("returncode") not in (0, 1, 5) and "No module named pytest" in r2.get("stderr", ""):
                    r2 = self._run([sys.executable, "-m", "unittest",
                                  test_file.replace("/", ".").replace(".py", "")],
                                 cwd=repo, env_paths=[str(self.eco_root), str(repo)])
                report["checks"]["unit_tests"] = r2
                if not r2["ok"]:
                    report["stage_failed"] = "unit_tests"
                    return report

            report["certified"] = True
            return report

    def _probe_script(self, module_name: str, class_name: str) -> str:
        return textwrap.dedent(f'''
            import json, sys
            try:
                from agents.{module_name}_agent import {class_name}
                a = {class_name}()
                a.start()
                assert getattr(a, "_started", False), "agent did not start"
                assert len(getattr(a, "capabilities", [])) > 0, "no capabilities"
                out = a.execute("definitely.not.a.task", {{}})
                assert isinstance(out, dict) and out.get("status") == "error", "unknown task not handled"
                a.stop()
                print(json.dumps({{"probe": "ok"}}))
            except Exception as exc:
                print(json.dumps({{"probe": "fail", "error": str(exc)}}))
                sys.exit(1)
        ''').strip()

    def _run(self, cmd: List[str], cwd: Path, env_paths: List[str],
             timeout: float = 30.0) -> Dict[str, Any]:
        import os
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(env_paths + [env.get("PYTHONPATH", "")])
        start = time.time()
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                                timeout=timeout, env=env)
            ok = proc.returncode == 0
            return {"ok": ok, "returncode": proc.returncode,
                   "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:],
                   "seconds": round(time.time() - start, 2)}
        except subprocess.TimeoutExpired:
            return {"ok": False, "returncode": -1, "error": f"timeout after {timeout}s"}
        except Exception as exc:
            return {"ok": False, "returncode": -1, "error": str(exc)}
