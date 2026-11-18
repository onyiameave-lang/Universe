from Oracle.execution.position_limits import OpenPositionBudget


def test_preexisting_unmanaged_positions_consume_the_account_wide_cap():
    budget = OpenPositionBudget(max_positions=3, observed_open_positions=2)

    assert budget.reserve() is True
    assert budget.open_count == 3
    assert budget.reserve() is False


def test_position_snapshot_reconciliation_cannot_create_a_new_slot_mid_cycle():
    budget = OpenPositionBudget(max_positions=3, observed_open_positions=1)

    assert budget.reserve() is True
    assert budget.reconcile(2) == 2
    assert budget.reserve() is True
    assert budget.reserve() is False


def test_only_definite_broker_rejection_releases_reserved_slot():
    budget = OpenPositionBudget(max_positions=3, observed_open_positions=2)

    assert budget.reserve() is True
    assert budget.full is True
    assert budget.release_rejected_order() == 2
    assert budget.reserve() is True
