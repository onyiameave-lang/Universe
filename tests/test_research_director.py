from Chronicle.tools import research_director


def test_filter_trades_since_previous_successful_report():
    trades = [
        {"timestamp": 99},
        {"timestamp": 100},
        {"timestamp": 150},
        {"timestamp": 201},
        {"timestamp": "150"},
        {},
    ]

    assert research_director._filter_trades_since(trades, 100, 200) == [
        {"timestamp": 150},
    ]
    assert research_director._filter_trades_since(trades, None, 200) == [
        {"timestamp": 99},
        {"timestamp": 100},
        {"timestamp": 150},
    ]


def test_failure_summary_reports_deltas_and_redacts_secrets():
    learning = {
        "failure_signatures": {
            "trade.propose::HTTP token=super-secret failed": 7,
            "market.analyze::timeout": 2,
        },
        "lessons": [{
            "task": "trade.propose",
            "root_cause": "timeout",
            "adjustment": "check network",
        }],
    }

    initial, counts, has_baseline = research_director._summarize_failures(
        learning, None)
    assert not has_baseline
    assert counts["trade.propose::HTTP token=super-secret failed"] == 7
    assert initial[0]["new_since_last_report"] is None
    assert "[REDACTED]" in initial[0]["error"]
    assert any(item.get("lesson") == "timeout" for item in initial)

    next_run, _, has_baseline = research_director._summarize_failures(
        learning, counts | {"trade.propose::HTTP token=super-secret failed": 5})
    assert has_baseline
    assert next_run[0]["new_since_last_report"] == 2


def test_state_checkpoint_round_trips_atomically(tmp_path):
    path = tmp_path / "state" / "director.json"
    state = {"last_successful_report_at": 123.5, "failure_signatures": {"x": 2}}

    assert research_director._save_state(state, path)
    assert research_director._load_state(path) == state
    assert not path.with_suffix(".tmp").exists()


def test_collect_learning_failures_reads_oracle_learning_log(tmp_path, monkeypatch):
    memory = tmp_path / "Oracle" / "memory"
    memory.mkdir(parents=True)
    (memory / "oracle_learning.json").write_text(
        '{"failure_signatures":{"trade.propose::timeout":4},"lessons":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(research_director, "_ECO_ROOT", tmp_path)

    data = research_director._collect_learning_failures()

    assert data["available"] is True
    assert data["failure_signatures"] == {"trade.propose::timeout": 4}


def test_hypothesis_reuses_inconclusive_work_before_adding():
    class Forge:
        def __init__(self):
            self.tasks = []

        def act(self, task, context):
            self.tasks.append(task)
            if task == "hypothesis.list":
                return {"hypotheses": [
                    {"id": "old", "statement": "question", "status": "confirmed"},
                    {"id": "current", "statement": "question",
                     "status": "inconclusive", "created_at": 2},
                ]}
            raise AssertionError("should reuse the inconclusive hypothesis")

    forge = Forge()
    assert research_director._get_or_add_hypothesis(forge, "question") == "current"
    assert forge.tasks == ["hypothesis.list"]


def test_hypothesis_adds_when_no_unresolved_match_exists():
    class Forge:
        def act(self, task, context):
            if task == "hypothesis.list":
                return {"hypotheses": [
                    {"id": "done", "statement": "question", "status": "rejected"},
                ]}
            assert task == "hypothesis.add"
            return {"hypothesis": {"id": "new"}}

    assert research_director._get_or_add_hypothesis(Forge(), "question") == "new"
