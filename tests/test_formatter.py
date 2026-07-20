from bot import formatter, mock_data


def test_fmt_compact():
    assert formatter.fmt_compact(9_960_000_000) == "9.96B"
    assert formatter.fmt_compact(1_970_000) == "1.97M"
    assert formatter.fmt_compact(14_100) == "14.1K"
    assert formatter.fmt_compact(None) == "?"


def test_fmt_money():
    assert formatter.fmt_money(45_488_286) == "$45,488,286"
    assert formatter.fmt_money(None) == "?"


def test_mug_alert_embed():
    e = formatter.build_mug_alert_embed(mock_data.mock_mug_event(1_700_000_000))
    assert "Mug opportunity" in e.title
    assert "Expected value" in e.description
    assert "Item: [Mag 7]" in e.description and "Damage: 66.86" in e.description
    assert "Attack!" in e.description and "Profile!" in e.description


def test_hospital_variant_embed():
    e = formatter.build_mug_alert_embed(mock_data.mock_hospital_mug_event(1_700_000_000))
    assert "Hospital" in e.title
    assert "Money on hand" in e.description
    assert "Was Mugged" in e.description


def test_status_and_deal_embeds():
    embeds = formatter.build_status_embeds(mock_data.mock_status(1_700_000_000))
    titles = " | ".join(e.title for e in embeds)
    blob = "\n".join(e.description for e in embeds)
    assert "Cache Status" in titles
    assert "Unmatched" in titles
    assert "📡" in blob and "API calls/min" in blob          # call-rate estimate shown
    # Watch list split into per-category messages.
    assert "Primary" in titles and "Secondary" in titles and "Melee" in titles and "Armour" in titles
    assert "BT MP9" in blob and "Yasukuni Sword" in blob and "Xanax" in blob
    assert "Can of Taurine Elite" in blob                    # unmatched item (name only)


def test_status_groups_split_by_category():
    groups = formatter.build_status_groups(mock_data.mock_status(1_700_000_000))
    keys = [k for k, _ in groups]
    # Ordered: cache -> Primary -> Secondary -> Melee -> Armour -> Items -> ... -> unmatched (last).
    assert keys[0] == "status_tracker"
    assert keys[-1] == "status_unmatched"
    for a, b in [("watch_Primary", "watch_Secondary"), ("watch_Secondary", "watch_Melee"),
                 ("watch_Melee", "watch_Defensive"), ("watch_Defensive", "watch_Items")]:
        assert keys.index(a) < keys.index(b)
    gmap = dict(groups)
    sec = "\n".join(e.description for e in gmap["watch_Secondary"])
    assert "BT MP9" in sec and "Yasukuni Sword" not in sec


def test_category_that_fits_shows_every_item():
    import time

    now = int(time.time())
    names = [f"{base} {part}" for base in ("Assault", "Combat", "Delta", "Dune", "EOD",
             "Marauder", "Riot", "Sentinel", "Vanguard")
             for part in ("Body", "Boots", "Gloves", "Helmet", "Pants", "Vest")]  # 54 items
    items = [{"item_id": 600 + i, "label": n, "enabled": False, "is_gear": True,
              "category": "Defensive", "captured": 0, "tracked": 0, "last_polled_ts": now - 1800}
             for i, n in enumerate(names)]
    embeds = formatter._watch_message("🛡️ Armour", items, now)
    blob = "\n".join(e.description for e in embeds)
    # No row dropped at the embed boundary, and the message stays under the 6000-char cap.
    assert all(n in blob for n in names), [n for n in names if n not in blob]
    assert sum(len(e.title or "") + len(e.description or "") for e in embeds) < 6000
    assert all(len(e.description or "") < 4096 for e in embeds)


def test_big_category_one_message_under_discord_limits():
    import time

    now = int(time.time())
    status = mock_data.mock_status(now)
    status["watchlist"] = [
        {"item_id": 1000 + i, "label": f"Some Long Weapon Name {i}", "enabled": True,
         "is_gear": True, "category": "Primary", "captured": 55, "tracked": 54,
         "last_polled_ts": now - 20}
        for i in range(120)
    ]
    groups = dict(formatter.build_status_groups(status))
    # Exactly ONE message per category (no spillover keys) ...
    assert "watch_Primary" in groups and "watch_Primary_2" not in groups
    # ... it truncates the overflow with a pointer ...
    primary = "\n".join(e.description for e in groups["watch_Primary"])
    assert "more" in primary and "/watch list" in primary
    # ... and EVERY message stays under Discord's 6000/message and 4096/embed caps.
    for embeds in groups.values():
        assert sum(formatter._dlen((e.title or "") + (e.description or "")) for e in embeds) < 6000
        assert all(formatter._dlen(e.description or "") < 4096 for e in embeds)

    d = formatter.build_deal_embed(mock_data.mock_deal(1_700_000_000))
    assert "Deal" in d.title
    assert "Buy on Market" in d.description
    assert "Price per item: **$425,000**" in d.description
    assert "Quantity: **3**" in d.description
    assert "Total cost: **$1,275,000**" in d.description


def test_alert_color_by_last_action():
    import time

    now = int(time.time())
    assert formatter._alert_color({"last_action_ts": now - 10}) == formatter.COLOR_RED      # <1m active
    assert formatter._alert_color({"last_action_ts": now - 3 * 60}) == formatter.COLOR_YELLOW  # 1–5m
    assert formatter._alert_color({"last_action_ts": now - 30 * 60}) == formatter.COLOR_GREEN  # >5m = go
    assert formatter._alert_color({}) == formatter.COLOR_GREEN                                 # unknown -> go


def test_hospital_and_overseas_are_blue():
    import time

    now = int(time.time())
    # Even if they acted seconds ago, hospital/overseas isn't actionable -> blue.
    assert formatter._alert_color({"variant": "hospital", "last_action_ts": now}) == formatter.COLOR_BLUE
    assert formatter._alert_color({"variant": "overseas", "last_action_ts": now}) == formatter.COLOR_BLUE
    assert formatter._alert_color({"status_state": "Traveling", "last_action_ts": now}) == formatter.COLOR_BLUE


def test_hospitalize_faction_embed():
    base = mock_data.mock_mug_event(1_700_000_000)
    e = formatter.build_mug_alert_embed(dict(base, hospitalize=True, faction="The Foo", faction_id=999))
    assert "HOSPITALIZE" in e.title
    assert "Hospitalize them, don't mug" in e.description and "The Foo" in e.description
    assert e.color.value == formatter.COLOR_HOSPITALIZE


def test_hospital_countdown_and_overseas_lines():
    base = mock_data.mock_mug_event(1_700_000_000)
    hosp = dict(base, variant="hospital", status_state="Hospital", status_until=1_700_000_900)
    e = formatter.build_mug_alert_embed(hosp)
    assert "Out of hospital:" in e.description

    over = dict(base, variant="overseas", status_state="Abroad", status_description="In Switzerland")
    e2 = formatter.build_mug_alert_embed(over)
    assert "Overseas" in e2.title and "In Switzerland" in e2.description
