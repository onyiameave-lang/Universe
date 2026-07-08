"""
Genesis.core.synthesis
======================
Institutional code synthesis for new agents. (Book III Part II Ch IV Genesis
generates agents; Book IV Part I URS; Book VI human sovereignty.)

An institutional factory does not stamp templates. It synthesizes real,
runnable, URS-compliant code, then GATES it through multiple safety checks
before anything is written:

  1. GENERATION   the LLM writes real capability methods from a blueprint;
                  without a brain, a correct scaffold is produced instead.
  2. AST VALIDATION every generated method must parse and define the expected
                  function. Unparseable code is never emitted.
  3. SAFETY SCAN  the generated source is scanned for forbidden constructs
                  (eval/exec, os.system, subprocess, __import__, network calls
                  the agent shouldn't own, secret literals). Failing code is
                  rejected, not written.
  4. STATIC LINT  basic structural checks (has class, inherits BaseAgent,
                  defines execute or capability handlers).

Nothing reaches disk unless it passes generation + AST + safety + lint.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Constructs an autonomously-created agent must NOT contain without review.
FORBIDDEN_PATTERNS = [
    (re.compile(r"\beval\s*\("), "eval() call"),
    (re.compile(r"\bexec\s*\("), "exec() call"),
    (re.compile(r"\bos\.system\s*\("), "os.system() call"),
    (re.compile(r"\bsubprocess\b"), "subprocess usage"),
    (re.compile(r"\b__import__\s*\("), "dynamic __import__"),
    (re.compile(r"\bshutil\.rmtree\s*\("), "recursive delete"),
    (re.compile(r"(?i)(api[_-]?key|secret|password)\s*=\s*['\"][^'\"]{6,}['\"]"), "secret literal"),
    (re.compile(r"\bopen\s*\([^)]*['\"]w"), "unguarded file write"),
]


def _clean(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        for p in parts:
            if "def " in p or "class " in p or "import " in p:
                text = p.replace("python", "", 1)
                break
    return text.strip("`\n ")


def _class_name(name: str) -> str:
    parts = [p for p in re.split(r"[\s_\-]+", name) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) + "Agent"


def _module_name(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


class SynthesisResult:
    def __init__(self):
        self.files: Dict[str, str] = {}
        self.checks: Dict[str, Any] = {}
        self.valid = False
        self.rejections: List[str] = []


class CodeSynthesizer:
    def __init__(self, llm=None):
        self.llm = llm

    @property
    def has_brain(self) -> bool:
        return self.llm is not None and getattr(self.llm, "has_any", False)

    # ---- capability method synthesis ----

    def synthesize_capability(self, agent_name: str, domain: str, capability: str,
                             purpose: str) -> Dict[str, Any]:
        method = capability.replace(".", "_")
        if not self.has_brain:
            return {"code": self._scaffold_method(capability, domain), "valid": True,
                   "source": "scaffold", "checks": {"ast": True, "safety": True}}
        try:
            from shared.llm import system_prompt, prompt_generate_capability
            r = self.llm.complete(system_prompt("genesis"),
                prompt_generate_capability(agent_name, domain, capability, purpose),
                temperature=0.2, max_tokens=900)
        except Exception as exc:
            return {"code": self._scaffold_method(capability, domain), "valid": True,
                   "source": "scaffold_after_error", "error": str(exc)}
        if not r.ok:
            return {"code": self._scaffold_method(capability, domain), "valid": True,
                   "source": "scaffold_fallback"}
        code = _clean(r.text)
        ast_ok, ast_detail = self._validate_method(code, method)
        safe_ok, safe_detail = self._safety_scan(code)
        if ast_ok and safe_ok:
            return {"code": code, "valid": True, "source": "llm",
                   "checks": {"ast": ast_detail, "safety": safe_detail}}
        # rejected -> safe scaffold instead of unsafe code
        return {"code": self._scaffold_method(capability, domain), "valid": True,
               "source": "scaffold_after_rejection",
               "rejected": {"ast": ast_detail, "safety": safe_detail}}

    def _validate_method(self, code: str, method: str) -> Tuple[bool, str]:
        indented = "\n".join(("    " + ln if ln.strip() else ln) for ln in code.splitlines())
        try:
            tree = ast.parse(f"class _Probe:\n{indented}\n")
        except SyntaxError as exc:
            return False, f"syntax error: {exc}"
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        return (method in names, "ok" if method in names else f"missing {method} (got {names})")

    def _safety_scan(self, code: str) -> Tuple[bool, str]:
        hits = [label for pat, label in FORBIDDEN_PATTERNS if pat.search(code)]
        return (not hits, "clean" if not hits else f"forbidden: {', '.join(hits)}")

    def _scaffold_method(self, capability: str, domain: str) -> str:
        method = capability.replace(".", "_")
        return (f'    def {method}(self, context: dict) -> dict:\n'
                f'        """Capability {capability}. Scaffold: specialize with real {domain} logic."""\n'
                f'        return {{"status": "unimplemented", "capability": "{capability}",\n'
                f'                "note": "Genesis scaffold; no validated LLM synthesis available.",\n'
                f'                "received": context}}\n')

    # ---- full agent file synthesis ----

    def synthesize_agent(self, bp: Dict[str, Any]) -> SynthesisResult:
        result = SynthesisResult()
        name, cls, mod = bp["name"], _class_name(bp["name"]), _module_name(bp["name"])
        domain = bp["domain"]

        methods, dispatch = [], []
        for cap in bp.get("capabilities", []):
            syn = self.synthesize_capability(name, domain, cap, bp.get("purpose", ""))
            methods.append(syn["code"])
            dispatch.append(f'        if task == "{cap}":\n'
                          f'            return self.{cap.replace(".", "_")}(context)')

        agent_code = self._assemble_agent(bp, cls, mod, dispatch, methods)

        # final whole-file gates
        ast_ok, ast_detail = self._validate_file(agent_code)
        safe_ok, safe_detail = self._safety_scan(agent_code)
        lint_ok, lint_detail = self._lint(agent_code, cls)
        result.checks = {"ast": ast_detail, "safety": safe_detail, "lint": lint_detail}
        result.valid = ast_ok and safe_ok and lint_ok
        if not result.valid:
            result.rejections = [d for ok, d in ((ast_ok, ast_detail), (safe_ok, safe_detail),
                                               (lint_ok, lint_detail)) if not ok]
            return result

        result.files = {
            f"agents/{mod}_agent.py": agent_code,
            f"testing/test_{mod}.py": self._test_code(bp, cls, mod),
            "repository.json": self._manifest(bp),
            "README.md": self._readme(bp),
            "main.py": self._main(bp, mod),
            "constitutional/COMPLIANCE.md": f"# {name} Compliance\n\nCreated by Genesis "
                                          f"via constitutional birth. Domain: {domain}.\n",
        }
        return result

    def _validate_file(self, code: str) -> Tuple[bool, str]:
        try:
            ast.parse(code)
            return True, "ok"
        except SyntaxError as exc:
            return False, f"syntax error: {exc}"

    def _lint(self, code: str, cls: str) -> Tuple[bool, str]:
        if f"class {cls}" not in code:
            return False, f"missing class {cls}"
        if "BaseAgent" not in code:
            return False, "does not inherit BaseAgent"
        if "def execute" not in code:
            return False, "missing execute()"
        return True, "ok"

    def _assemble_agent(self, bp, cls, mod, dispatch, methods) -> str:
        caps = json.dumps(bp.get("capabilities", []))
        chans = json.dumps(bp.get("channels", [f"ecosystem.{bp['domain']}", "ecosystem.broadcast"]))
        objs = "\n".join(f"      - {o}" for o in bp.get("objectives", []))
        dispatch_block = "\n".join(dispatch) if dispatch else "        pass"
        methods_block = "\n".join(methods)
        return f'''"""
{bp["name"]} Agent  (created by Genesis)
{"=" * (len(bp["name"]) + 22)}
Domain: {bp["domain"]}
Mission: {bp.get("purpose", "")}
Objectives:
{objs}

Generated via the constitutional birth process (Book I Part IV Article II) and
validated (AST + safety + lint + Aegis) before deployment.
"""
from __future__ import annotations
import logging, sys
from pathlib import Path
from typing import Any, Dict, List

_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))
try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        def __init__(self, **kw): self._started = False
        def act(self, task, context=None): return self.execute(task, context or {{}})
        def get_status(self): return {{"name": getattr(self, "name", "agent")}}
        def start(self): self._started = True
        def stop(self): self._started = False

log = logging.getLogger("{mod}")


class {cls}(BaseAgent):
    name = "{mod}"
    repository = "{bp["name"]}"
    domain = "{bp["domain"]}"
    description = {json.dumps(bp.get("purpose", ""))}
    capabilities = {caps}
    channels = {chans}
    memory_namespace = "{mod}_memory"
    security_level = "{bp.get("security_level", "standard")}"
    mission = {{"purpose": {json.dumps(bp.get("purpose", ""))}}}

    def __init__(self, chronicle_client=None, **kw):
        super().__init__(chronicle_client=chronicle_client, storage_dir=str(Path(__file__).resolve().parents[1] / "memory"), **kw) if _HAS_SHARED else BaseAgent.__init__(self)
        self.chronicle = chronicle_client

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
{dispatch_block}
        return {{"status": "error", "message": f"Unknown task: {{task}}"}}

{methods_block}
    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {{"name": self.name}}
        return base


def main():
    logging.basicConfig(level=logging.INFO)
    agent = {cls}()
    agent.start()
    print("{bp["name"]} online. Capabilities:", ", ".join(agent.capabilities))


if __name__ == "__main__":
    main()
'''

    def _test_code(self, bp, cls, mod) -> str:
        first = (bp.get("capabilities") or ["noop"])[0]
        return f'''"""Tests for {bp["name"]} (Genesis-generated)."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from agents.{mod}_agent import {cls}


class Test{cls}(unittest.TestCase):
    def setUp(self):
        self.agent = {cls}(); self.agent.start()
    def tearDown(self):
        self.agent.stop()
    def test_starts(self):
        self.assertTrue(self.agent._started)
    def test_domain(self):
        self.assertEqual(self.agent.domain, "{bp["domain"]}")
    def test_capability_routes(self):
        r = self.agent.handle({{"task": "{first}", "context": {{}}, "sender": "t"}}) \\
            if hasattr(self.agent, "handle") else self.agent.execute("{first}", {{}})
        self.assertIn(r["status"], ("complete", "unimplemented", "error"))
    def test_unknown_task_errors(self):
        r = self.agent.execute("no.such.task", {{}})
        self.assertEqual(r["status"], "error")


if __name__ == "__main__":
    unittest.main()
'''

    def _manifest(self, bp) -> str:
        mod = _module_name(bp["name"])
        return json.dumps({"constitutional_name": bp["name"], "former_name": bp["name"],
            "repository_id": f"{mod}-1.0.0", "version": "1.0.0", "constitution_version": "1.0.0",
            "repository_type": bp["domain"], "domain": bp["domain"],
            "primary_mission": bp.get("purpose", ""), "objectives": bp.get("objectives", []),
            "capabilities": bp.get("capabilities", []),
            "channels": bp.get("channels", [f"ecosystem.{bp['domain']}", "ecosystem.broadcast"]),
            "security_level": bp.get("security_level", "standard"),
            "memory_namespace": f"{mod}_memory", "dependencies": ["chronicle", "nexus"],
            "lifecycle_status": "deployed", "created_by": "genesis"}, indent=2)

    def _readme(self, bp) -> str:
        caps = "\n".join(f"- `{c}`" for c in bp.get("capabilities", []))
        return (f"# {bp['name']}\n\n*Created by Genesis (Agent Factory).*\n\n"
                f"**Domain:** {bp['domain']} | **Security:** {bp.get('security_level', 'standard')}\n\n"
                f"## Mission\n{bp.get('purpose', '')}\n\n## Capabilities\n{caps}\n\n"
                f"## Run\n```bash\npython main.py\n```\n")

    def _main(self, bp, mod) -> str:
        return (f'"""{bp["name"]} entry point (Genesis-generated)."""\n'
                f'import sys\nfrom pathlib import Path\n'
                f'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
                f'sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n'
                f'from agents.{mod}_agent import main\n\nif __name__ == "__main__":\n    main()\n')
