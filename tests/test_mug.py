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


def test_a_sale_the_seller_has_been_online_since_is_dropped_not_discounted():
    """The rule that replaced a linear decay. Under the decay a sale from 9 minutes ago still
    carried ~60% of its value, which is how hospital alerts kept quoting hundreds of millions of
    already-banked cash. Banking is not gradual: they either went to the bank or they didn't, and
    every second of activity after the sale is a chance they did."""
    now = 1_000_000
    sales = [
        {"total_value": 100, "sold_at": now},           # sold at their last action -> still on hand
        {"total_value": 100, "sold_at": now - 400},     # active ~7m after -> banked
        {"total_value": 100, "sold_at": now - 99999},   # active long after -> banked
    ]
    on_hand, banked = mug.split_banked(sales, last_action_ts=now, grace=180)
    assert [s["sold_at"] for s in on_hand] == [now]
    assert len(banked) == 2
    assert mug.bank_adjusted_total(sales, last_action_ts=now, grace=180) == 100


def test_the_grace_covers_clock_skew_only():
    """`sold_at` is when the poller SAW the listing vanish, not when the buyer clicked, so an action
    a minute either side of the sale is noise rather than a trip to the bank."""
    now = 1_000_000
    inside = [{"total_value": 100, "sold_at": now - 179}]
    outside = [{"total_value": 100, "sold_at": now - 181}]
    assert mug.bank_adjusted_total(inside, last_action_ts=now, grace=180) == 100
    assert mug.bank_adjusted_total(outside, last_action_ts=now, grace=180) == 0


def test_no_last_action_drops_nothing():
    """An assumption that deletes the entire value of an alert needs evidence behind it. Not knowing
    when someone last acted is not evidence that they banked."""
    sales = [{"total_value": 100, "sold_at": 1}, {"total_value": 200, "sold_at": 2}]
    on_hand, banked = mug.split_banked(sales, last_action_ts=None)
    assert banked == [] and len(on_hand) == 2
    assert mug.bank_adjusted_total(sales, last_action_ts=None) == 300


def test_the_split_is_a_clean_cut_in_time():
    """Relied on by the caller: every banked sale is older than every kept one, so claiming the
    newest banked sale can never swallow one we are still alerting on."""
    now = 1_000_000
    sales = [{"total_value": 1, "sold_at": now - t} for t in (5000, 900, 300, 100, 0)]
    on_hand, banked = mug.split_banked(sales, last_action_ts=now, grace=180)
    assert max(s["sold_at"] for s in banked) < min(s["sold_at"] for s in on_hand)
