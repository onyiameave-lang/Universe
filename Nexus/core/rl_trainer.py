"""
Nexus.core.rl_trainer
=====================
RL / imitation-learning training pipeline for Nexus routing.

CYCLE:
  1. Gemini routes queries → decisions logged to routing_decisions.jsonl
  2. After >= MIN_TRAINING_SAMPLES decisions, run_training_cycle() is called
  3. Training data is converted to Ollama Modelfile fine-tune format
  4. Local Ollama model is updated via Ollama's /api/create endpoint
  5. Accuracy is evaluated: does local model agree with Gemini on held-out queries?
  6. If accuracy >= ACCURACY_THRESHOLD, local model is promoted to Tier 1
  7. Cycle repeats — local model keeps improving as more decisions are logged

OLLAMA FINE-TUNING APPROACH:
  Ollama does not expose a gradient-based fine-tune API. Instead we use the
  Modelfile FROM + SYSTEM + MESSAGE approach to create a specialised model
  variant that has the routing examples baked into its system prompt and
  few-shot examples. This is "prompt-based imitation learning" — not gradient
  descent, but it achieves the same goal: the local model learns to replicate
  Gemini's routing decisions from examples.

  For true gradient fine-tuning, the training data is also exported as a
  standard JSONL dataset (Alpaca format) that can be used with llama.cpp,
  Unsloth, or any other fine-tuning framework.

ENVIRONMENT VARIABLES:
  NEXUS_ROUTER_LOG      — Path to routing decisions JSONL (default Nexus/memory/routing_decisions.jsonl)
  NEXUS_MIN_SAMPLES     — Minimum logged decisions before training (default 100)
  NEXUS_ACCURACY_THRESH — Minimum accuracy to promote local model (default 0.80)
  NEXUS_TRAIN_MODEL     — Name for the trained local routing model (default nexus-router)
  OLLAMA_URL            — Local Ollama base URL (default http://localhost:11434)
  OLLAMA_MODEL          — Base model to fine-tune from (e.g. llama3, mistral)

CONSTITUTIONAL COMPLIANCE:
  Book II Principle V    Everything Evolves — routing improves via RL loop.
  Book II Principle VI   Nothing Dies Without Leaving Knowledge — training data
                         is preserved in Alpaca JSONL for future use.
  Book IV Continuous Improvement — accuracy metrics tracked across cycles.
  Book II No Silent Failures — all training steps logged at INFO/WARNING.

FIX LOG:
  RL-01  Initial implementation (2026-07-21).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("nexus.rl_trainer")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "memory" / "routing_decisions.jsonl"
_LOG_PATH: Path = Path(os.getenv("NEXUS_ROUTER_LOG", str(_DEFAULT_LOG_PATH)))

_DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[1] / "memory" / "routing_dataset.jsonl"
_DEFAULT_METRICS_PATH = Path(__file__).resolve().parents[1] / "memory" / "training_metrics.jsonl"
_DEFAULT_MODELFILE_PATH = Path(__file__).resolve().parents[1] / "memory" / "NexusRouterModelfile"

_MIN_TRAINING_SAMPLES: int = int(os.getenv("NEXUS_MIN_SAMPLES", "100"))
_ACCURACY_THRESHOLD: float = float(os.getenv("NEXUS_ACCURACY_THRESH", "0.80"))
_TRAIN_MODEL_NAME: str = os.getenv("NEXUS_TRAIN_MODEL", "nexus-router")
_OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
_OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "").strip()

# Fraction of data held out for accuracy evaluation
_EVAL_FRACTION: float = 0.15
# Maximum few-shot examples to embed in the Modelfile system prompt
_MAX_FEW_SHOT: int = 20


# ---------------------------------------------------------------------------
# Training data builder
# ---------------------------------------------------------------------------
class TrainingDataBuilder:
    """Convert logged routing decisions into training formats.

    Produces:
      1. Alpaca JSONL — standard fine-tuning format for llama.cpp / Unsloth
      2. Ollama Modelfile — prompt-based imitation learning via Ollama /api/create
      3. Train / eval split for accuracy measurement

    Constitutional law: Book II Principle VI Nothing Dies Without Leaving
    Knowledge — all training data is persisted to disk.
    """

    _INSTRUCTION = (
        "You are the routing brain of Nexus. Given a user query, return a JSON "
        "routing plan that extracts the subject, corrects typos, and chooses the "
        "right specialist agent. Return ONLY valid JSON."
    )

    def __init__(self, log_path: Path = _LOG_PATH):
        self._log_path = log_path

    def load_decisions(self, min_quality: float = 0.0) -> List[Dict[str, Any]]:
        """Load all logged decisions, optionally filtered by quality score.

        Decisions with quality=None (not yet reinforced) are included when
        min_quality=0.0 (default) — Gemini's routing is trusted as ground truth
        even without explicit quality feedback.
        """
        if not self._log_path.exists():
            return []
        records = []
        try:
            for line in self._log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    q = rec.get("quality")
                    # Include if: quality is None (Gemini decision, trusted) OR quality >= min_quality
                    if q is None or q >= min_quality:
                        records.append(rec)
                except Exception:
                    continue
        except Exception as exc:
            log.warning("TrainingDataBuilder.load_decisions failed: %s", exc)
        return records

    def build_alpaca_dataset(
        self,
        decisions: List[Dict[str, Any]],
        output_path: Path = _DEFAULT_DATASET_PATH,
    ) -> int:
        """Export decisions as Alpaca-format JSONL for gradient fine-tuning.

        Format per line:
        {"instruction": "...", "input": "<query>", "output": "<json plan>"}

        Constitutional law: Book II Principle VI Nothing Dies Without Leaving
        Knowledge — dataset persisted for future fine-tuning frameworks.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        try:
            with output_path.open("w", encoding="utf-8") as f:
                for rec in decisions:
                    query = rec.get("query", "").strip()
                    plan = rec.get("plan", {})
                    if not query or not plan:
                        continue
                    # Produce a clean, minimal plan for the output
                    output_plan = {
                        "symbol": plan.get("symbol", ""),
                        "topic": plan.get("topic", ""),
                        "primary_domain": plan.get("primary_domain", "general"),
                        "primary_agent": plan.get("primary_agent", "atlas"),
                        "primary_task": plan.get("primary_task", ""),
                        "multi_agent": plan.get("multi_agent", False),
                        "agents": plan.get("agents", []),
                        "synthesis_strategy": plan.get("synthesis_strategy", "ollama_first"),
                        "confidence": plan.get("confidence", 0.85),
                    }
                    record = {
                        "instruction": self._INSTRUCTION,
                        "input": query,
                        "output": json.dumps(output_plan, ensure_ascii=False),
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
            log.info(
                "TrainingDataBuilder: exported %d Alpaca records to %s. "
                "Constitutional: Book II Principle VI Nothing Dies Without Leaving Knowledge.",
                count, output_path,
            )
        except Exception as exc:
            log.warning("TrainingDataBuilder.build_alpaca_dataset failed: %s", exc)
        return count

    def build_modelfile(
        self,
        decisions: List[Dict[str, Any]],
        base_model: str = _OLLAMA_MODEL,
        output_path: Path = _DEFAULT_MODELFILE_PATH,
        max_few_shot: int = _MAX_FEW_SHOT,
    ) -> Optional[Path]:
        """Build an Ollama Modelfile with few-shot routing examples.

        The Modelfile uses FROM <base_model> and embeds the top few-shot
        examples as MESSAGE pairs in the system prompt. This is Ollama's
        supported mechanism for creating specialised model variants.

        Returns the path to the written Modelfile, or None on failure.

        Constitutional law: Book II Principle V Everything Evolves — the
        Modelfile is rebuilt on every training cycle with the latest examples.
        """
        if not base_model:
            log.warning(
                "TrainingDataBuilder.build_modelfile: OLLAMA_MODEL not set — "
                "cannot build Modelfile. Set OLLAMA_MODEL in .env."
            )
            return None

        # Select the highest-quality examples for few-shot
        scored = []
        for rec in decisions:
            q = rec.get("quality")
            score = q if q is not None else 0.85  # Gemini decisions trusted at 0.85
            scored.append((score, rec))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [rec for _, rec in scored[:max_few_shot]]

        # Build few-shot MESSAGE blocks
        message_blocks = []
        for rec in top:
            query = rec.get("query", "").strip()
            plan = rec.get("plan", {})
            if not query or not plan:
                continue
            output_plan = {
                "symbol": plan.get("symbol", ""),
                "topic": plan.get("topic", ""),
                "primary_domain": plan.get("primary_domain", "general"),
                "primary_agent": plan.get("primary_agent", "atlas"),
                "primary_task": plan.get("primary_task", ""),
                "multi_agent": plan.get("multi_agent", False),
                "agents": plan.get("agents", []),
                "synthesis_strategy": plan.get("synthesis_strategy", "ollama_first"),
                "confidence": plan.get("confidence", 0.85),
            }
            message_blocks.append(
                f'MESSAGE user "{query}"\n'
                f'MESSAGE assistant {json.dumps(json.dumps(output_plan, ensure_ascii=False))}'
            )

        system_prompt = (
            "You are the routing brain of Nexus, an institutional AI coordinator. "
            "Given a user query, return a JSON routing plan. "
            "Extract the ACTUAL subject (ticker/company/topic) — NEVER return the full query as symbol. "
            "Correct typos: nvida→NVDA, appl→AAPL. "
            "Return ONLY valid JSON with keys: symbol, topic, primary_domain, primary_agent, "
            "primary_task, multi_agent, agents, synthesis_strategy, confidence."
        )

        modelfile_content = (
            f"FROM {base_model}\n\n"
            f"SYSTEM \"\"\"\n{system_prompt}\n\"\"\"\n\n"
            + "\n\n".join(message_blocks)
        )

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(modelfile_content, encoding="utf-8")
            log.info(
                "TrainingDataBuilder.build_modelfile: wrote Modelfile with %d few-shot examples to %s.",
                len(message_blocks), output_path,
            )
            return output_path
        except Exception as exc:
            log.warning("TrainingDataBuilder.build_modelfile failed: %s", exc)
            return None

    def train_eval_split(
        self,
        decisions: List[Dict[str, Any]],
        eval_fraction: float = _EVAL_FRACTION,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split decisions into train and eval sets.

        Eval set is the most recent eval_fraction of decisions (chronological
        split — avoids data leakage from future decisions into training).
        """
        if not decisions:
            return [], []
        n_eval = max(1, int(len(decisions) * eval_fraction))
        # Sort by timestamp (most recent last)
        sorted_d = sorted(decisions, key=lambda r: r.get("ts", 0))
        train = sorted_d[:-n_eval]
        eval_ = sorted_d[-n_eval:]
        return train, eval_


# ---------------------------------------------------------------------------
# Ollama model trainer
# ---------------------------------------------------------------------------
class OllamaTrainer:
    """Create/update the local routing model via Ollama's /api/create endpoint.

    Ollama's /api/create accepts a Modelfile and creates a new named model.
    We use this to create 'nexus-router' (or NEXUS_TRAIN_MODEL) from the
    base model with few-shot routing examples embedded.

    Constitutional law: Book II Principle V Everything Evolves — the local
    model is rebuilt on every training cycle.
    """

    def __init__(
        self,
        url: str = _OLLAMA_URL,
        model_name: str = _TRAIN_MODEL_NAME,
    ):
        self._url = url
        self._model_name = model_name

    def create_model(self, modelfile_path: Path, timeout: float = 120.0) -> bool:
        """Create/update the routing model from a Modelfile.

        Returns True on success, False on failure.

        Constitutional law: Book II No Silent Failures — all errors logged.
        """
        if not modelfile_path.exists():
            log.warning("OllamaTrainer.create_model: Modelfile not found at %s", modelfile_path)
            return False

        modelfile_content = modelfile_path.read_text(encoding="utf-8")
        payload = json.dumps({
            "name": self._model_name,
            "modelfile": modelfile_content,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self._url}/api/create",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            status = data.get("status", "")
            if "success" in status.lower() or status == "":
                log.info(
                    "OllamaTrainer: model '%s' created/updated successfully. "
                    "Constitutional: Book II Principle V Everything Evolves.",
                    self._model_name,
                )
                return True
            log.warning("OllamaTrainer.create_model: unexpected status=%r", status)
            return False
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            log.warning(
                "OllamaTrainer.create_model: HTTP %d — %s. "
                "Constitutional: Book II No Silent Failures.",
                exc.code, body,
            )
            return False
        except Exception as exc:
            log.warning("OllamaTrainer.create_model failed: %s", exc)
            return False

    def model_exists(self, timeout: float = 5.0) -> bool:
        """Check if the routing model already exists in Ollama."""
        req = urllib.request.Request(
            f"{self._url}/api/tags",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            return any(self._model_name in m for m in models)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Accuracy evaluator
# ---------------------------------------------------------------------------
class AccuracyEvaluator:
    """Evaluate local model routing accuracy against Gemini ground truth.

    For each eval query, calls the local model and compares its routing plan
    to the Gemini-logged plan. Agreement is measured on:
      - primary_agent match (most important)
      - symbol match (important for news/trading)
      - primary_domain match (secondary)

    Constitutional law: Book IV Continuous Improvement — accuracy tracked
    across training cycles so we know when local model is ready.
    """

    def __init__(self, url: str = _OLLAMA_URL, model_name: str = _TRAIN_MODEL_NAME):
        self._url = url
        self._model_name = model_name

    def evaluate(
        self,
        eval_decisions: List[Dict[str, Any]],
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Evaluate local model on held-out decisions.

        Returns accuracy metrics dict.
        """
        if not eval_decisions:
            return {"accuracy": 0.0, "n_eval": 0, "agent_match": 0.0, "symbol_match": 0.0}

        agent_matches = 0
        symbol_matches = 0
        domain_matches = 0
        n = 0

        for rec in eval_decisions:
            query = rec.get("query", "").strip()
            expected_plan = rec.get("plan", {})
            if not query or not expected_plan:
                continue

            predicted = self._call_local_model(query, timeout)
            if predicted is None:
                continue

            n += 1
            if predicted.get("primary_agent") == expected_plan.get("primary_agent"):
                agent_matches += 1
            if predicted.get("symbol", "").upper() == expected_plan.get("symbol", "").upper():
                symbol_matches += 1
            if predicted.get("primary_domain") == expected_plan.get("primary_domain"):
                domain_matches += 1

        if n == 0:
            return {"accuracy": 0.0, "n_eval": 0, "agent_match": 0.0, "symbol_match": 0.0}

        agent_acc = agent_matches / n
        symbol_acc = symbol_matches / n
        domain_acc = domain_matches / n
        # Overall accuracy: weighted average (agent most important)
        overall = 0.5 * agent_acc + 0.3 * symbol_acc + 0.2 * domain_acc

        return {
            "accuracy": round(overall, 4),
            "n_eval": n,
            "agent_match": round(agent_acc, 4),
            "symbol_match": round(symbol_acc, 4),
            "domain_match": round(domain_acc, 4),
        }

    def _call_local_model(self, query: str, timeout: float) -> Optional[Dict[str, Any]]:
        """Call the local routing model and parse its JSON response."""
        prompt = (
            f"System: You are the routing brain of Nexus. Return a JSON routing plan.\n\n"
            f"User: {query}\n\nAssistant:"
        )
        payload = json.dumps({
            "model": self._model_name,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 256},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self._url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data.get("response", "").strip()
            s, e = text.find("{"), text.rfind("}")
            if s != -1 and e > s:
                return json.loads(text[s:e + 1])
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Metrics logger
# ---------------------------------------------------------------------------
class MetricsLogger:
    """Append training cycle metrics to a JSONL file.

    Constitutional law: Book IV Continuous Improvement — metrics tracked
    across cycles so we can observe the RL loop converging.
    """

    def __init__(self, path: Path = _DEFAULT_METRICS_PATH):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, cycle: int, metrics: Dict[str, Any]) -> None:
        record = {"ts": time.time(), "cycle": cycle, **metrics}
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("MetricsLogger.log failed: %s", exc)

    def load(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        records = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass
        return records

    def last_cycle(self) -> int:
        records = self.load()
        if not records:
            return 0
        return max(r.get("cycle", 0) for r in records)


# ---------------------------------------------------------------------------
# RLTrainer — the public interface
# ---------------------------------------------------------------------------
class RLTrainer:
    """Orchestrates the full RL/imitation-learning training cycle.

    Usage:
        trainer = RLTrainer()
        result = trainer.run_training_cycle()
        # result["promoted"] == True means local model is now Tier 1

    Constitutional law:
      Book II Principle V    Everything Evolves — routing improves via RL loop.
      Book II Principle VI   Nothing Dies Without Leaving Knowledge — all data persisted.
      Book IV Continuous Improvement — metrics tracked across cycles.
      Book II No Silent Failures — all steps logged.
    """

    def __init__(
        self,
        log_path: Path = _LOG_PATH,
        min_samples: int = _MIN_TRAINING_SAMPLES,
        accuracy_threshold: float = _ACCURACY_THRESHOLD,
        base_model: str = _OLLAMA_MODEL,
        train_model_name: str = _TRAIN_MODEL_NAME,
        ollama_url: str = _OLLAMA_URL,
    ):
        self._log_path = log_path
        self._min_samples = min_samples
        self._accuracy_threshold = accuracy_threshold
        self._base_model = base_model
        self._train_model_name = train_model_name

        self._builder = TrainingDataBuilder(log_path)
        self._trainer = OllamaTrainer(ollama_url, train_model_name)
        self._evaluator = AccuracyEvaluator(ollama_url, train_model_name)
        self._metrics = MetricsLogger()

    def run_training_cycle(self) -> Dict[str, Any]:
        """Run one full training cycle.

        Steps:
          1. Load logged decisions
          2. Check if enough data (>= min_samples)
          3. Split into train / eval
          4. Export Alpaca JSONL dataset
          5. Build Ollama Modelfile with few-shot examples
          6. Create/update local model via Ollama /api/create
          7. Evaluate accuracy on held-out eval set
          8. Log metrics
          9. Return result dict with "promoted" flag

        Returns dict with keys:
          n_decisions, n_train, n_eval, accuracy, promoted, cycle, message
        """
        cycle = self._metrics.last_cycle() + 1
        log.info(
            "RLTrainer: starting training cycle %d. "
            "Constitutional: Book II Principle V Everything Evolves.",
            cycle,
        )

        # Step 1: Load decisions
        decisions = self._builder.load_decisions(min_quality=0.0)
        n_total = len(decisions)
        log.info("RLTrainer: loaded %d routing decisions from %s.", n_total, self._log_path)

        # Step 2: Check minimum samples
        if n_total < self._min_samples:
            msg = (
                f"Not enough training data: {n_total}/{self._min_samples} decisions logged. "
                f"Keep using Gemini routing — more decisions will be logged automatically."
            )
            log.info("RLTrainer: %s", msg)
            return {
                "n_decisions": n_total,
                "n_train": 0,
                "n_eval": 0,
                "accuracy": 0.0,
                "promoted": False,
                "cycle": cycle,
                "message": msg,
            }

        # Step 3: Train / eval split
        train_data, eval_data = self._builder.train_eval_split(decisions)
        log.info("RLTrainer: train=%d eval=%d", len(train_data), len(eval_data))

        # Step 4: Export Alpaca JSONL
        n_exported = self._builder.build_alpaca_dataset(train_data)

        # Step 5: Build Modelfile
        if not self._base_model:
            msg = "OLLAMA_MODEL not set — cannot build Modelfile. Set OLLAMA_MODEL in .env."
            log.warning("RLTrainer: %s", msg)
            return {
                "n_decisions": n_total, "n_train": len(train_data),
                "n_eval": len(eval_data), "accuracy": 0.0,
                "promoted": False, "cycle": cycle, "message": msg,
            }

        modelfile_path = self._builder.build_modelfile(train_data, self._base_model)
        if modelfile_path is None:
            msg = "Modelfile build failed — see logs for details."
            log.warning("RLTrainer: %s", msg)
            return {
                "n_decisions": n_total, "n_train": len(train_data),
                "n_eval": len(eval_data), "accuracy": 0.0,
                "promoted": False, "cycle": cycle, "message": msg,
            }

        # Step 6: Create/update local model
        model_ok = self._trainer.create_model(modelfile_path)
        if not model_ok:
            msg = (
                f"Ollama model creation failed for '{self._train_model_name}'. "
                f"Is Ollama running at {_OLLAMA_URL}?"
            )
            log.warning("RLTrainer: %s", msg)
            return {
                "n_decisions": n_total, "n_train": len(train_data),
                "n_eval": len(eval_data), "accuracy": 0.0,
                "promoted": False, "cycle": cycle, "message": msg,
            }

        # Step 7: Evaluate accuracy
        metrics = self._evaluator.evaluate(eval_data)
        accuracy = metrics.get("accuracy", 0.0)
        promoted = accuracy >= self._accuracy_threshold

        log.info(
            "RLTrainer: cycle %d complete. accuracy=%.2f%% (threshold=%.0f%%) promoted=%s. "
            "agent_match=%.2f%% symbol_match=%.2f%% domain_match=%.2f%%. "
            "Constitutional: Book IV Continuous Improvement.",
            cycle,
            accuracy * 100, self._accuracy_threshold * 100, promoted,
            metrics.get("agent_match", 0) * 100,
            metrics.get("symbol_match", 0) * 100,
            metrics.get("domain_match", 0) * 100,
        )

        if promoted:
            log.info(
                "RLTrainer: LOCAL MODEL PROMOTED to Tier 1. "
                "Set OLLAMA_MODEL=%s in .env to use it as the primary routing model. "
                "Constitutional: Book II Principle V Everything Evolves.",
                self._train_model_name,
            )
        else:
            log.info(
                "RLTrainer: local model accuracy %.2f%% < threshold %.0f%% — "
                "Gemini remains Tier 1. Continue logging decisions and re-run training.",
                accuracy * 100, self._accuracy_threshold * 100,
            )

        # Step 8: Log metrics
        self._metrics.log(cycle, {
            "n_decisions": n_total,
            "n_train": len(train_data),
            "n_eval": len(eval_data),
            "n_exported": n_exported,
            "accuracy": accuracy,
            "promoted": promoted,
            **{k: v for k, v in metrics.items() if k != "accuracy"},
        })

        msg = (
            f"Cycle {cycle}: accuracy={accuracy:.1%} "
            f"({'PROMOTED to Tier 1' if promoted else 'not yet promoted — keep logging'}). "
            f"Train={len(train_data)} Eval={len(eval_data)} Total={n_total}."
        )
        return {
            "n_decisions": n_total,
            "n_train": len(train_data),
            "n_eval": len(eval_data),
            "accuracy": accuracy,
            "promoted": promoted,
            "cycle": cycle,
            "message": msg,
            "metrics": metrics,
        }

    def status(self) -> Dict[str, Any]:
        """Return current training status without running a cycle."""
        decisions = self._builder.load_decisions()
        n = len(decisions)
        last_metrics = self._metrics.load()
        last = last_metrics[-1] if last_metrics else {}
        return {
            "logged_decisions": n,
            "min_samples_needed": self._min_samples,
            "ready_to_train": n >= self._min_samples,
            "last_cycle": last.get("cycle", 0),
            "last_accuracy": last.get("accuracy", 0.0),
            "last_promoted": last.get("promoted", False),
            "model_exists": self._trainer.model_exists(),
            "base_model": self._base_model,
            "train_model_name": self._train_model_name,
        }