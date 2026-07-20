"""
Item id <-> name maps for /watch autocomplete + friendly labels.

Consumables (stackables) come from config.ITEM_MAP; the gear (weapon/armour) name->id map is
below (mirrors the item-matcher's ITEM_ID_MAP). Used to resolve `/watch add <name|id>`, hint
whether an id is gear, and label the watch-list panel before the poller has seen the item.
"""

import config

GEAR_NAME_TO_ID = {
    "Flexible Body Armor": 334, "Liquid Body Armor": 333, "Combat Vest": 332, "Combat Helmet": 651,
    "Combat Pants": 652, "Combat Boots": 653, "Combat Gloves": 654, "Outer Tactical Vest": 50,
    "Kevlar Gloves": 640, "WWII Helmet": 641, "Safety Boots": 645, "Full Body Armor": 49,
    "Bulletproof Vest": 34, "Construction Helmet": 643, "Police Vest": 33, "Flak Jacket": 178,
    "Hiking Boots": 646, "Chain Mail": 176, "Leather Vest": 32, "Leather Helmet": 647,
    "Leather Pants": 648, "Leather Boots": 649, "Leather Gloves": 650, "Vanguard Gloves": 1359,
    "Vanguard Boots": 1358, "Vanguard Pants": 1357, "Vanguard Body": 1356, "Vanguard Respirator": 1355,
    "Sentinel Gloves": 1311, "Sentinel Boots": 1310, "Sentinel Pants": 1309, "Sentinel Apron": 1308,
    "Sentinel Helmet": 1307, "Starshield Breastplate": 1174, "M'aol Clawshields": 1168,
    "M'aol Hooves": 1167, "M'aol Britches": 1166, "M'aol Spathe": 1165, "M'aol Visage": 1164,
    "Kevlar Lab Coat": 848, "EOD Gloves": 684, "EOD Boots": 683, "EOD Pants": 682, "EOD Apron": 681,
    "EOD Helmet": 680, "Marauder Gloves": 679, "Marauder Boots": 678, "Marauder Pants": 677,
    "Marauder Body": 676, "Marauder Face Mask": 675, "Delta Gloves": 674, "Delta Boots": 673,
    "Delta Pants": 672, "Delta Body": 671, "Delta Gas Mask": 670, "Assault Gloves": 669,
    "Assault Boots": 668, "Assault Pants": 667, "Assault Body": 666, "Assault Helmet": 665,
    "Dune Gloves": 664, "Dune Boots": 663, "Dune Pants": 662, "Dune Vest": 661, "Dune Helmet": 660,
    "Riot Gloves": 659, "Riot Boots": 658, "Riot Pants": 657, "Riot Body": 656, "Riot Helmet": 655,
    "Welding Helmet": 644, "Motorcycle Helmet": 642, "Medieval Helmet": 538, "Hazmat Suit": 348,
    "Gold Plated AK-47": 382, "ArmaLite M-15A4": 399, "SIG 552": 398, "Jackhammer": 223,
    "Minigun": 63, "9mm Uzi": 108, "M249 SAW": 31, "Tavor TAR-21": 612, "Enfield SA-80": 219,
    "Steyr AUG": 30, "Mag 7": 225, "M16 A2 Rifle": 29, "Heckler & Koch SL8": 231,
    "Benelli M4 Super": 28, "M4A1 Colt Carbine": 27, "Bushmaster Carbon 15": 241, "AK-47": 26,
    "XM8 Rifle": 174, "Ithaca 37": 252, "P90": 25, "Vektor CR-21": 228, "SKS Carbine": 249,
    "MP5 Navy": 24, "Benelli M1 Tactical": 23, "Sawed-Off Shotgun": 22, "Stoner 96": 1157,
    "Negev NG-5": 1156, "PKM": 1155, "Rheinmetall MG 3": 837, "Nock Gun": 830, "Dual Uzis": 549,
    "Dual P90s": 548, "Dual MP5s": 547, "Dual Bushmasters": 546, "Dual TMPs": 545, "MP 40": 488,
    "Thompson": 487, "AK74U": 484, "SIG 550": 232, "Egg Propelled Launcher": 100, "Neutrilux 2000": 98,
    "Snow Cannon": 76, "Handbag": 387, "Naval Cutlass": 615, "Pillow": 440, "Flail": 397,
    "Diamond Bladed Knife": 614, "Metal Nunchaku": 395, "Guandao": 400, "Macana": 391,
    "Claymore Sword": 217, "Kodachi": 237, "Wushu Double Axes": 251, "Samurai Sword": 11,
    "Twin Tiger Hooks": 250, "Kama": 236, "Ice Pick": 402, "Katana": 247, "Cricket Bat": 438,
    "Chainsaw": 10, "Scimitar": 9, "Ninja Claws": 111, "Wooden Nunchaku": 235, "Axe": 8,
    "Chain Whip": 234, "Swiss Army Knife": 224, "Dagger": 7, "Butterfly Knife": 173,
    "Leather Bullwhip": 110, "Kitchen Knife": 6, "Sai": 238, "Pen Knife": 5, "Knuckle Dusters": 4,
    "Spear": 227, "Bo Staff": 245, "Frying Pan": 439, "Crowbar": 3, "Baseball Bat": 2, "Lead Pipe": 401,
    "Hammer": 1, "Ban Hammer": 1296, "Cattle Prod": 1257, "Bone Saw": 1255, "Golf Club": 1231,
    "Crystalline Falcata": 1173, "Cleaver": 1159, "Meat Hook": 1158, "Millwall Brick": 1056,
    "Poison Umbrella": 1055, "Bread Knife": 1053, "Bug Swatter": 871, "Sledgehammer": 850,
    "Scalpel": 846, "Bolt Gun": 845, "Madball": 839, "Riding Crop": 832, "Duke's Hammer": 805,
    "Penelope": 792, "Plastic Sword": 790, "Petrified Humerus": 632, "Diamond Icicle": 605,
    "Pair of Ice Skates": 604, "Devil's Pitchfork": 600, "Golden Broomstick": 599,
    "Blood Spattered Sickle": 539, "Ivory Walking Cane": 360, "Fine Chisel": 359,
    "Pair of High Heels": 346, "Dual Samurai Swords": 292, "Dual Scimitars": 291, "Dual Hammers": 290,
    "Dual Axes": 289, "Wand of Destruction": 170, "Rusty Sword": 147, "Yasukuni Sword": 146,
    "Pink Mac-10": 388, "Type 98 Anti Tank": 240, "Flamethrower": 255, "Harpoon": 613, "Qsz-92": 248,
    "Cobra Derringer": 177, "BT MP9": 233, "Desert Eagle": 20, "Magnum": 19, "Fiveseven": 18,
    "Taser": 175, "USP": 16, "Blowgun": 244, "Beretta M9": 15, "Ruger 57": 14, "Crossbow": 218,
    "Taurus": 243, "Raven MP25": 13, "Springfield 1911": 99, "Glock 17": 12, "Lorcin 380": 253,
    "Flare Gun": 230, "Milkor MGL": 1154, "China Lake": 1153, "SMAW Launcher": 1152, "Prototype": 874,
    "Tranquilizer Gun": 844, "Homemade Pocket Shotgun": 838, "Beretta Pico": 831, "Blunderbuss": 490,
    "Luger": 489, "TMP": 486, "Skorpion": 485, "MP5k": 483, "Slingshot": 393, "S&W M29": 254,
    "S&W Revolver": 189, "RPG Launcher": 109, "Dual 92G Berettas": 21, "Beretta 92FS": 17,
}

GEAR_IDS = set(GEAR_NAME_TO_ID.values())
CONSUMABLE_IDS = set(config.ITEM_MAP.keys())

# id -> name (gear + consumables)
ID_TO_NAME = {**{v: k for k, v in GEAR_NAME_TO_ID.items()}, **config.ITEM_MAP}
# lowercase name -> id
NAME_TO_ID = {name.lower(): iid for iid, name in ID_TO_NAME.items()}
# (id, name) sorted by name, for autocomplete
CATALOG = sorted(ID_TO_NAME.items(), key=lambda kv: kv[1].lower())


def name_for(item_id):
    return ID_TO_NAME.get(item_id, f"Item {item_id}")


def is_gear_hint(item_id):
    """True/False if known, else None (poller will detect)."""
    if item_id in GEAR_IDS:
        return True
    if item_id in CONSUMABLE_IDS:
        return False
    return None


def resolve(text):
    """'233' or 'bt mp9' -> (item_id, name). (None, None) if unresolved."""
    text = (text or "").strip()
    if not text:
        return None, None
    if text.isdigit():
        iid = int(text)
        return iid, ID_TO_NAME.get(iid)
    iid = NAME_TO_ID.get(text.lower())
    return (iid, ID_TO_NAME.get(iid)) if iid is not None else (None, None)
