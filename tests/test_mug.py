from server import mug


def test_basic_range_and_qualifies():
    r = mug.compute_mug(100_000_000, pct_min=0.10, pct_max=0.20, min_value=5_000_000)
    assert r.mug_min == 10_000_000
    assert r.mug_max == 20_000_000
    assert r.qualifies is True


def test_clothing_store_reduces_and_self_suppresses():
    r = mug.compute_mug(
        100_000_000, in_clothing_store=True,
        pct_min=0.10, pct_max=0.20, clothing_mult=0.25, min_value=5_000_000,
    )
    assert r.clothing_mult == 0.25
    assert r.mug_min == 2_500_000      # 75% reduction
    assert r.mug_max == 5_000_000
    assert r.qualifies is False        # 2.5M < 5M threshold -> no alert


def test_threshold_gates_small_sales():
    r = mug.compute_mug(10_000_000, pct_min=0.10, pct_max=0.20, min_value=5_000_000)
    assert r.mug_min == 1_000_000
    assert r.qualifies is False


def test_matches_reference_embed_5_to_15_pct():
    # "Mr. Blobby" reference embed: value change 45,488,286 at 5%-15%.
    r = mug.compute_mug(45_488_286, pct_min=0.05, pct_max=0.15)
    assert r.mug_min == 2_274_414
    assert r.mug_max == 6_823_243


def test_default_pcts_are_5_to_15():
    # Defaults now match the reference bot (5%-15%), so compute_mug() with no overrides agrees.
    r = mug.compute_mug(45_488_286)
    assert r.mug_min == 2_274_414
    assert r.mug_max == 6_823_243


def test_handles_none_and_negative():
    r = mug.compute_mug(None)
    assert r.mug_min == 0 and r.mug_max == 0 and r.qualifies is False
    r2 = mug.compute_mug(-5)
    assert r2.mug_min == 0 and r2.mug_max == 0


def test_bank_adjusted_total_decays_with_activity():
    from server import mug
    now = 1_000_000
    sales = [
        {"total_value": 100, "sold_at": now},          # last action right at the sale -> full
        {"total_value": 100, "sold_at": now - 3600},   # active 1h after -> partial
        {"total_value": 100, "sold_at": now - 99999},  # active long after -> ~0 (banked)
    ]
    # grace 15m, assume 6h
    total = mug.bank_adjusted_total(sales, last_action_ts=now, grace=900, assume=21600)
    assert 100 <= total < 250            # first full, second partial, third ~0
    # No last_action info -> count everything.
    assert mug.bank_adjusted_total(sales, last_action_ts=None) == 300
