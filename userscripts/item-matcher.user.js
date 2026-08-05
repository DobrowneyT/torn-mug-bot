// ==UserScript==
// @name         Item Matcher
// @version      1.17
// @description  Matches the seller of items for the Mug Bot
// @author       MonChoon
// @match        https://www.torn.com/page.php?sid=ItemMarket*
// @run-at       document-start
// @icon         https://www.google.com/s2/favicons?sz=64&domain/torn.com
// @grant        GM.xmlHttpRequest
// @grant        GM.getValue
// @grant        GM.setValue
// @grant        GM.registerMenuCommand
// @connect      51.68.212.98
// ==/UserScript==

(function() {
    'use strict';

    const ITEM_ID_MAP = {
      "Flexible Body Armor": 334,
      "Liquid Body Armor": 333,
      "Combat Vest": 332,
      "Combat Helmet": 651,
      "Combat Pants": 652,
      "Combat Boots": 653,
      "Combat Gloves": 654,
      "Outer Tactical Vest": 50,
      "Kevlar Gloves": 640,
      "WWII Helmet": 641,
      "Safety Boots": 645,
      "Full Body Armor": 49,
      "Bulletproof Vest": 34,
      "Construction Helmet": 643,
      "Police Vest": 33,
      "Flak Jacket": 178,
      "Hiking Boots": 646,
      "Chain Mail": 176,
      "Leather Vest": 32,
      "Leather Helmet": 647,
      "Leather Pants": 648,
      "Leather Boots": 649,
      "Leather Gloves": 650,
      "Vanguard Gloves": 1359,
      "Vanguard Boots": 1358,
      "Vanguard Pants": 1357,
      "Vanguard Body": 1356,
      "Vanguard Respirator": 1355,
      "Sentinel Gloves": 1311,
      "Sentinel Boots": 1310,
      "Sentinel Pants": 1309,
      "Sentinel Apron": 1308,
      "Sentinel Helmet": 1307,
      "Starshield Breastplate": 1174,
      "M'aol Clawshields": 1168,
      "M'aol Hooves": 1167,
      "M'aol Britches": 1166,
      "M'aol Spathe": 1165,
      "M'aol Visage": 1164,
      "Kevlar Lab Coat": 848,
      "EOD Gloves": 684,
      "EOD Boots": 683,
      "EOD Pants": 682,
      "EOD Apron": 681,
      "EOD Helmet": 680,
      "Marauder Gloves": 679,
      "Marauder Boots": 678,
      "Marauder Pants": 677,
      "Marauder Body": 676,
      "Marauder Face Mask": 675,
      "Delta Gloves": 674,
      "Delta Boots": 673,
      "Delta Pants": 672,
      "Delta Body": 671,
      "Delta Gas Mask": 670,
      "Assault Gloves": 669,
      "Assault Boots": 668,
      "Assault Pants": 667,
      "Assault Body": 666,
      "Assault Helmet": 665,
      "Dune Gloves": 664,
      "Dune Boots": 663,
      "Dune Pants": 662,
      "Dune Vest": 661,
      "Dune Helmet": 660,
      "Riot Gloves": 659,
      "Riot Boots": 658,
      "Riot Pants": 657,
      "Riot Body": 656,
      "Riot Helmet": 655,
      "Welding Helmet": 644,
      "Motorcycle Helmet": 642,
      "Medieval Helmet": 538,
      "Hazmat Suit": 348,
      "Gold Plated AK-47": 382,
      "ArmaLite M-15A4": 399,
      "SIG 552": 398,
      "Jackhammer": 223,
      "Minigun": 63,
      "9mm Uzi": 108,
      "M249 SAW": 31,
      "Tavor TAR-21": 612,
      "Enfield SA-80": 219,
      "Steyr AUG": 30,
      "Mag 7": 225,
      "M16 A2 Rifle": 29,
      "Heckler & Koch SL8": 231,
      "Benelli M4 Super": 28,
      "M4A1 Colt Carbine": 27,
      "Bushmaster Carbon 15": 241,
      "AK-47": 26,
      "XM8 Rifle": 174,
      "Ithaca 37": 252,
      "P90": 25,
      "Vektor CR-21": 228,
      "SKS Carbine": 249,
      "MP5 Navy": 24,
      "Benelli M1 Tactical": 23,
      "Sawed-Off Shotgun": 22,
      "Stoner 96": 1157,
      "Negev NG-5": 1156,
      "PKM": 1155,
      "Rheinmetall MG 3": 837,
      "Nock Gun": 830,
      "Dual Uzis": 549,
      "Dual P90s": 548,
      "Dual MP5s": 547,
      "Dual Bushmasters": 546,
      "Dual TMPs": 545,
      "MP 40": 488,
      "Thompson": 487,
      "AK74U": 484,
      "SIG 550": 232,
      "Egg Propelled Launcher": 100,
      "Neutrilux 2000": 98,
      "Snow Cannon": 76,
      "Handbag": 387,
      "Naval Cutlass": 615,
      "Pillow": 440,
      "Flail": 397,
      "Diamond Bladed Knife": 614,
      "Metal Nunchaku": 395,
      "Guandao": 400,
      "Macana": 391,
      "Claymore Sword": 217,
      "Kodachi": 237,
      "Wushu Double Axes": 251,
      "Samurai Sword": 11,
      "Twin Tiger Hooks": 250,
      "Kama": 236,
      "Ice Pick": 402,
      "Katana": 247,
      "Cricket Bat": 438,
      "Chainsaw": 10,
      "Scimitar": 9,
      "Ninja Claws": 111,
      "Wooden Nunchaku": 235,
      "Axe": 8,
      "Chain Whip": 234,
      "Swiss Army Knife": 224,
      "Dagger": 7,
      "Butterfly Knife": 173,
      "Leather Bullwhip": 110,
      "Kitchen Knife": 6,
      "Sai": 238,
      "Pen Knife": 5,
      "Knuckle Dusters": 4,
      "Spear": 227,
      "Bo Staff": 245,
      "Frying Pan": 439,
      "Crowbar": 3,
      "Baseball Bat": 2,
      "Lead Pipe": 401,
      "Hammer": 1,
      "Ban Hammer": 1296,
      "Cattle Prod": 1257,
      "Bone Saw": 1255,
      "Golf Club": 1231,
      "Crystalline Falcata": 1173,
      "Cleaver": 1159,
      "Meat Hook": 1158,
      "Millwall Brick": 1056,
      "Poison Umbrella": 1055,
      "Bread Knife": 1053,
      "Bug Swatter": 871,
      "Sledgehammer": 850,
      "Scalpel": 846,
      "Bolt Gun": 845,
      "Madball": 839,
      "Riding Crop": 832,
      "Duke's Hammer": 805,
      "Penelope": 792,
      "Plastic Sword": 790,
      "Petrified Humerus": 632,
      "Diamond Icicle": 605,
      "Pair of Ice Skates": 604,
      "Devil's Pitchfork": 600,
      "Golden Broomstick": 599,
      "Blood Spattered Sickle": 539,
      "Ivory Walking Cane": 360,
      "Fine Chisel": 359,
      "Pair of High Heels": 346,
      "Dual Samurai Swords": 292,
      "Dual Scimitars": 291,
      "Dual Hammers": 290,
      "Dual Axes": 289,
      "Wand of Destruction": 170,
      "Rusty Sword": 147,
      "Yasukuni Sword": 146,
      "Pink Mac-10": 388,
      "Type 98 Anti Tank": 240,
      "Flamethrower": 255,
      "Harpoon": 613,
      "Qsz-92": 248,
      "Cobra Derringer": 177,
      "BT MP9": 233,
      "Desert Eagle": 20,
      "Magnum": 19,
      "Fiveseven": 18,
      "Taser": 175,
      "USP": 16,
      "Blowgun": 244,
      "Beretta M9": 15,
      "Ruger 57": 14,
      "Crossbow": 218,
      "Taurus": 243,
      "Raven MP25": 13,
      "Springfield 1911": 99,
      "Glock 17": 12,
      "Lorcin 380": 253,
      "Flare Gun": 230,
      "Milkor MGL": 1154,
      "China Lake": 1153,
      "SMAW Launcher": 1152,
      "Prototype": 874,
      "Tranquilizer Gun": 844,
      "Homemade Pocket Shotgun": 838,
      "Beretta Pico": 831,
      "Blunderbuss": 490,
      "Luger": 489,
      "TMP": 486,
      "Skorpion": 485,
      "MP5k": 483,
      "Slingshot": 393,
      "S&W M29": 254,
      "S&W Revolver": 189,
      "RPG Launcher": 109,
      "Dual 92G Berettas": 21,
      "Beretta 92FS": 17
    };

    const ARMOR_NAMES = new Set([
      "Flexible Body Armor", "Liquid Body Armor", "Combat Vest", "Combat Helmet", "Combat Pants",
      "Combat Boots", "Combat Gloves", "Outer Tactical Vest", "Kevlar Gloves", "WWII Helmet",
      "Safety Boots", "Full Body Armor", "Bulletproof Vest", "Construction Helmet", "Police Vest",
      "Flak Jacket", "Hiking Boots", "Chain Mail", "Leather Vest", "Leather Helmet", "Leather Pants",
      "Leather Boots", "Leather Gloves", "Vanguard Gloves", "Vanguard Boots", "Vanguard Pants",
      "Vanguard Body", "Vanguard Respirator", "Sentinel Gloves", "Sentinel Boots", "Sentinel Pants",
      "Sentinel Apron", "Sentinel Helmet", "Starshield Breastplate", "M'aol Clawshields", "M'aol Hooves",
      "M'aol Britches", "M'aol Spathe", "M'aol Visage", "Kevlar Lab Coat", "EOD Gloves", "EOD Boots",
      "EOD Pants", "EOD Apron", "EOD Helmet", "Marauder Gloves", "Marauder Boots", "Marauder Pants",
      "Marauder Body", "Marauder Face Mask", "Delta Gloves", "Delta Boots", "Delta Pants", "Delta Body",
      "Delta Gas Mask", "Assault Gloves", "Assault Boots", "Assault Pants", "Assault Body",
      "Assault Helmet", "Dune Gloves", "Dune Boots", "Dune Pants", "Dune Vest", "Dune Helmet",
      "Riot Gloves", "Riot Boots", "Riot Pants", "Riot Body", "Riot Helmet", "Welding Helmet",
      "Motorcycle Helmet", "Medieval Helmet", "Hazmat Suit"
    ]);

    // Consolidated: anon + regular gear now share ONE server/DB (the old split into a
    // separate :3002 anon server only existed because the original server was unavailable).
    //
    // The address is a SETTING, not a constant. It used to be a hardcoded constant in tracked
    // source, and THIS script is the cautionary tale: when the box moved, this file kept pointing
    // at the retired one and every gear capture went into the void for weeks — silently, because a
    // network error is one line in a log panel nobody was reading. See #15 / #37.
    //
    // Kept in localStorage rather than GM storage so market-tracker reads the SAME value; the two
    // scripts share an origin but not GM storage, and them drifting apart is the original defect.
    // The constant below is only the fresh-install fallback.
    const SERVER_URL_KEY = 'mugBotServerUrl';
    const SERVER_URL_DEFAULT = 'http://51.68.212.98:3001';

    function serverUrl() {
        const stored = (localStorage.getItem(SERVER_URL_KEY) || '').trim().replace(/\/+$/, '');
        return /^https?:\/\//.test(stored) ? stored : SERVER_URL_DEFAULT;
    }

    // DEBUG: when true, the script does NOT POST to the server. Instead it logs the raw
    // getListing response + the exact payload it WOULD send to the console as JSON, so we can
    // inspect the schema (e.g. the gear `uid`) before deciding what to store. Flip to false to
    // resume normal server submission.
    const DEBUG_CONSOLE_ONLY = false;

    // VERBOSE: when true, log the full JSON of everything sent to / received from the server into
    // the script's Log panel (handy for debugging the matching flow). Leave false for quiet logs.
    const VERBOSE = false;

    // Functions, not constants: the address can change at runtime (menu command / status pill),
    // and a constant captured at load would keep posting to the old box until a page reload.
    const MAIN_SUBMIT_URL = () => `${serverUrl()}/gear`;
    const MAIN_UNCACHED_URL = () => `${serverUrl()}/cache/uncached`;
    const MAIN_GEAR_CACHE_URL = () => `${serverUrl()}/cache/gear`;

    const ANON_SUBMIT_URL = () => `${serverUrl()}/gear/anon`;
    const ANON_GEAR_CACHE_URL = () => `${serverUrl()}/cache/gear/anon`;

    const SEND_QUEUE_INTERVAL = 500;
    const GEAR_CACHE_EXPIRY_MS = 10 * 60 * 1000;
    // The item-market API can't refresh faster than ~30s, so don't poll the server faster than that.
    const AUTO_FETCH_INTERVAL_MS = 30 * 1000;

    // Torn's CSS-module class hashes (the `___xxxxx` suffix) ROTATE on every site deploy. Matching
    // the exact hash silently breaks all highlighting + scraping when that happens (which is the
    // "Failed to scrape item details" / "nothing highlights" symptom). Match the stable prefix
    // instead so the script keeps working across Torn redeploys.
    const SEL = {
        tile:       '[class*="itemTile___"]',
        name:       '[class*="name___"]',
        value:      '[class*="value___"]',
        property:   '[class*="property___"]',
        properties: '[class*="properties___"]',
        expanded:   '[class*="expanded___"]',
        bonuses:    '[class*="bonuses___"]',
        icon:       '[class*="icon___"]',
    };

    let PRICE_FILTER_PERCENTAGE = 10;

    let pda = ('xmlhttpRequest' in GM);
    let httpRequest = pda ? 'xmlhttpRequest' : 'xmlHttpRequest';

    let gearCacheIDs = new Set();
    let anonCacheIDs = new Set();
    let listingQueue = [];
    let anonListingQueue = [];
    let itemsSentCount = 0;
    let anonItemsSentCount = 0;
    let debugLog = [];

    let isScriptEnabled = true;
    let autoFetchEnabled = true;
    let autoFetchTimer = null;
    let isMinimized = false;
    let isDebugLogVisible = false;
    let isUncachedListVisible = false;
    let isSettingsVisible = false;
    let uncachedSortBy = 'price';
    let uncachedSortOrder = 'asc';

    let uncachedItems = [];
    let lastFetchTime = null;
    let isFetching = false;
    let selectedSortedItemIndex = -1;

    // "Easy" capture walker (see easyButtonStep). Two presses per item: navigate, then open the
    // listing (clicks the Buy cart, which EXPANDS the panel to reveal the seller + fires getListing —
    // it does NOT purchase; that needs a separate confirm inside the panel, which we never touch).
    let easyPhase = 'navigate';   // 'navigate' (next press goes to the item) | 'open' (expands it)
    let easyIndex = 0;            // position in the current sorted uncached list
    let easyNavItem = null;       // the item we navigated to (opened on the next press)

    let gearCacheItems = [];
    let gearCacheLastFetch = null;
    let isFetchingGearCache = false;

    let anonGearCacheItems = [];
    let anonGearCacheLastFetch = null;
    let isFetchingAnonGearCache = false;
    let isAnonServerAvailable = true;
    // The MAIN server's reachability. The anon server has had this since it was a separate box; the
    // main one never did, which is exactly why this script pointing at a retired host went unnoticed
    // for weeks. Not persisted: on a fresh page load the honest answer is "unknown", and the first
    // request settles it within seconds.
    let isMainServerAvailable = true;

    /** Record whether the main server is answering, and surface it if that changed. */
    function promptForServerUrl() {
        const next = prompt('Mug bot server address\n\n' +
                            '(shared with the market-tracker userscript)', serverUrl());
        if (next === null) return;
        const trimmed = next.trim().replace(/\/+$/, '');
        if (trimmed && !/^https?:\/\//.test(trimmed)) {
            alert('That does not look like a URL — it needs to start with http:// or https://');
            return;
        }
        if (trimmed) {
            localStorage.setItem(SERVER_URL_KEY, trimmed);
        } else {
            localStorage.removeItem(SERVER_URL_KEY);   // blank = back to the built-in default
        }
        isMainServerAvailable = true;      // unknown again until the next request says otherwise
        addDebugLog(`Server set to ${serverUrl()}`);
        updateStatsUI();
        fetchUncachedItems();              // prove it immediately rather than at the next tick
    }

    function markMainServer(ok, why) {
        if (ok === isMainServerAvailable) return;
        isMainServerAvailable = ok;
        addDebugLog(ok ? `\u2713 server reachable again (${serverUrl()})`
                       : `\u26a0 SERVER UNREACHABLE: ${serverUrl()} — ${why}`);
        updateStatsUI();
    }
    let anonServerLastCheckTime = null;

    let prevUncachedCount = 0;
    let prevGearCacheCount = 0;
    let prevAnonCacheCount = 0;

    let prevGreenHighlights = 0;

    let processedListingIDs = new Set();

    let statsUI = null;

    function addDebugLog(message) {
        const timestamp = new Date().toLocaleTimeString();
        debugLog.unshift(`[${timestamp}] ${message}`);
        if (debugLog.length > 50) debugLog.pop();
        updateStatsUI();
    }

    // Verbose log: full sent/received JSON, only when VERBOSE is on.
    function vlog(message) {
        if (VERBOSE) addDebugLog('🔍 ' + message);
    }

    async function loadScriptState() {
        isScriptEnabled = await GM.getValue('itemMatcherEnabled', true);
        autoFetchEnabled = await GM.getValue('autoFetchEnabled', true);
        isDebugLogVisible = await GM.getValue('isDebugLogVisible', false);
        isUncachedListVisible = await GM.getValue('isUncachedListVisible', false);
        isSettingsVisible = await GM.getValue('isSettingsVisible', false);
        uncachedSortBy = await GM.getValue('uncachedSortBy', 'price');
        uncachedSortOrder = await GM.getValue('uncachedSortOrder', 'asc');
        isMinimized = await GM.getValue('isMinimized', false);

        PRICE_FILTER_PERCENTAGE = await GM.getValue('priceFilterPercentage', 10);

        uncachedItems = await GM.getValue('uncachedItems', []);
        const storedFetchTime = await GM.getValue('lastFetchTime', null);
        lastFetchTime = storedFetchTime ? new Date(storedFetchTime) : null;

        gearCacheItems = await GM.getValue('gearCacheItems', []);
        const storedGearFetchTime = await GM.getValue('gearCacheLastFetch', null);
        gearCacheLastFetch = storedGearFetchTime ? new Date(storedGearFetchTime) : null;

        anonGearCacheItems = await GM.getValue('anonGearCacheItems', []);
        const storedAnonGearFetchTime = await GM.getValue('anonGearCacheLastFetch', null);
        anonGearCacheLastFetch = storedAnonGearFetchTime ? new Date(storedAnonGearFetchTime) : null;

        isAnonServerAvailable = await GM.getValue('isAnonServerAvailable', true);
        const storedAnonServerCheckTime = await GM.getValue('anonServerLastCheckTime', null);
        anonServerLastCheckTime = storedAnonServerCheckTime ? new Date(storedAnonServerCheckTime) : null;

        addDebugLog(`Script loaded. Enabled: ${isScriptEnabled}. Loaded ${uncachedItems.length} uncached + ${gearCacheItems.length} gear + ${anonGearCacheItems.length} anon items.`);
    }

    async function toggleMinimize() {
        isMinimized = !isMinimized;
        await GM.setValue('isMinimized', isMinimized);
        updateStatsUI();
    }

    async function toggleScript() {
        isScriptEnabled = !isScriptEnabled;
        await GM.setValue('itemMatcherEnabled', isScriptEnabled);
        addDebugLog(`Script toggled: ${isScriptEnabled ? 'ON' : 'OFF'}`);
        if (isScriptEnabled) {
            startAutoFetch();
            highlightMatchingItems();
        } else {
            // Fully stop: no auto-fetch, no in-flight queues, no tile highlights.
            stopAutoFetch();
            listingQueue = [];
            anonListingQueue = [];
            clearTileHighlights();
        }
        updateStatsUI();
    }

    async function toggleAutoFetch() {
        autoFetchEnabled = !autoFetchEnabled;
        await GM.setValue('autoFetchEnabled', autoFetchEnabled);
        addDebugLog(`Auto-fetch ${autoFetchEnabled ? 'ON' : 'OFF'}`);
        if (autoFetchEnabled && isScriptEnabled) {
            startAutoFetch();
        } else {
            stopAutoFetch();
        }
        updateStatsUI();
    }

    // Pull all three server caches at once (manual button + the auto-fetch loop both use this).
    async function fetchAllCaches() {
        if (!isScriptEnabled) return;
        await fetchUncachedItems();
        await fetchGearCache();
        if (isAnonServerAvailable) await fetchAnonGearCache();
    }

    function startAutoFetch() {
        stopAutoFetch();
        if (!autoFetchEnabled || !isScriptEnabled) return;
        autoFetchTimer = setInterval(() => {
            if (autoFetchEnabled && isScriptEnabled) fetchAllCaches();
        }, AUTO_FETCH_INTERVAL_MS);
    }

    function stopAutoFetch() {
        if (autoFetchTimer) {
            clearInterval(autoFetchTimer);
            autoFetchTimer = null;
        }
    }

    // Does this tile carry our green "uncached" border? Chrome serialises an inline `#00ff00` back
    // out as `rgb(0, 255, 0)`, so a literal `.includes('00ff00')` silently fails — that's what made
    // the Easy walker see "0 green" and target the wrong tile, and it also broke the idempotency
    // check below (borders re-painted every mutation). Match either form.
    function isGreenTile(tile) {
        const b = (tile.style && tile.style.border) || '';
        return b.includes('00ff00') || b.includes('0, 255, 0');
    }

    // Remove any borders this script painted on item tiles (used when toggling OFF).
    function clearTileHighlights() {
        document.querySelectorAll(SEL.tile).forEach(tile => {
            if (isGreenTile(tile) || tile.style.border.includes('0088ff')) {
                tile.style.border = '';
            }
        });
    }

    async function toggleDebugLog() {
        isDebugLogVisible = !isDebugLogVisible;
        await GM.setValue('isDebugLogVisible', isDebugLogVisible);
        updateStatsUI();
    }

    async function toggleUncachedList() {
        isUncachedListVisible = !isUncachedListVisible;
        selectedSortedItemIndex = -1;
        await GM.setValue('isUncachedListVisible', isUncachedListVisible);
        updateStatsUI();
    }

    async function toggleSettings() {
        isSettingsVisible = !isSettingsVisible;
        await GM.setValue('isSettingsVisible', isSettingsVisible);
        updateStatsUI();
    }

    async function updatePriceFilter(value) {
        const numValue = parseInt(value);
        if (isNaN(numValue)) {
            addDebugLog('Invalid price filter value');
            return;
        }

        if (numValue < -1 || numValue > 100) {
            addDebugLog('Price filter must be -1 (disabled) or 0-100');
            return;
        }

        PRICE_FILTER_PERCENTAGE = numValue;
        await GM.setValue('priceFilterPercentage', numValue);
        addDebugLog(`Price filter updated: ${numValue === -1 ? 'DISABLED' : numValue + '%'}`);
        updateStatsUI();
    }

    async function setUncachedSort(sortBy) {
        if (uncachedSortBy === sortBy) {
            uncachedSortOrder = uncachedSortOrder === 'asc' ? 'desc' : 'asc';
        } else {
            uncachedSortBy = sortBy;
            uncachedSortOrder = 'asc';
        }
        selectedSortedItemIndex = -1;
        await GM.setValue('uncachedSortBy', uncachedSortBy);
        await GM.setValue('uncachedSortOrder', uncachedSortOrder);
        updateStatsUI();
    }

    function isOnItemMarketPage() {
        return window.location.href.includes('page.php?sid=ItemMarket');
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }


    async function fetchUncachedItems() {
        if (isFetching) {
            addDebugLog('Fetch already in progress...');
            return;
        }

        isFetching = true;
        addDebugLog('Fetching uncached items...');
        updateStatsUI();

        GM[httpRequest]({
            method: 'GET',
            url: MAIN_UNCACHED_URL(),
            responseType: 'json',
            onload: async function(response) {
                isFetching = false;
                if (response.status === 200) {
                    try {
                        const data = response.response || JSON.parse(response.responseText);
                        uncachedItems = data.listings || [];
                        lastFetchTime = new Date();
                        selectedSortedItemIndex = -1;
                        markMainServer(true);
                        addDebugLog(`✓ Fetched ${uncachedItems.length} uncached items`);
                        vlog('← /cache/uncached: ' + JSON.stringify(uncachedItems));

                        await GM.setValue('uncachedItems', uncachedItems);
                        await GM.setValue('lastFetchTime', lastFetchTime.toISOString());

                        updateStatsUI();
                        highlightMatchingItems();
                    } catch (err) {
                        addDebugLog(`✗ Parse error: ${err.message}`);
                        updateStatsUI();
                    }
                } else {
                    markMainServer(false, `HTTP ${response.status}`);
                    addDebugLog(`✗ Fetch failed (HTTP ${response.status})`);
                    updateStatsUI();
                }
            },
            onerror: function() {
                isFetching = false;
                markMainServer(false, 'network error');
                addDebugLog(`✗ Network error fetching uncached items`);
                updateStatsUI();
            }
        });
    }

    async function fetchGearCache(forceRefresh = false) {
        const now = Date.now();

        if (!forceRefresh && gearCacheItems.length > 0 && gearCacheLastFetch && (now - gearCacheLastFetch) < GEAR_CACHE_EXPIRY_MS) {
            return;
        }

        if (isFetchingGearCache) {
            return;
        }

        isFetchingGearCache = true;
        if (forceRefresh) {
            addDebugLog('Force refreshing gear cache...');
        }
        updateStatsUI();

        GM[httpRequest]({
            method: 'GET',
            url: MAIN_GEAR_CACHE_URL(),
            responseType: 'json',
            onload: async function(response) {
                isFetchingGearCache = false;
                if (response.status === 200) {
                    try {
                        const data = response.response || JSON.parse(response.responseText);
                        gearCacheItems = data.listings || [];
                        gearCacheLastFetch = new Date();
                        markMainServer(true);
                        addDebugLog(`✓ Fetched ${gearCacheItems.length} gear cache items`);

                        await GM.setValue('gearCacheItems', gearCacheItems);
                        await GM.setValue('gearCacheLastFetch', gearCacheLastFetch.toISOString());

                        updateStatsUI();
                        highlightMatchingItems();
                    } catch (err) {
                        addDebugLog(`✗ Gear cache parse error: ${err.message}`);
                        updateStatsUI();
                    }
                } else {
                    markMainServer(false, `HTTP ${response.status}`);
                    addDebugLog(`✗ Gear cache fetch failed (HTTP ${response.status})`);
                    updateStatsUI();
                }
            },
            onerror: function() {
                isFetchingGearCache = false;
                markMainServer(false, 'network error');
                addDebugLog(`✗ Network error fetching gear cache`);
                updateStatsUI();
            }
        });
    }

    async function fetchAnonGearCache(forceRefresh = false) {
        if (!isAnonServerAvailable && !forceRefresh) {
            return;
        }

        const now = Date.now();

        if (!forceRefresh && anonGearCacheItems.length > 0 && anonGearCacheLastFetch && (now - anonGearCacheLastFetch) < GEAR_CACHE_EXPIRY_MS) {
            return;
        }

        if (isFetchingAnonGearCache) {
            return;
        }

        isFetchingAnonGearCache = true;
        if (forceRefresh) {
            addDebugLog('Force refreshing anon gear cache...');
        }
        updateStatsUI();

        GM[httpRequest]({
            method: 'GET',
            url: ANON_GEAR_CACHE_URL(),
            responseType: 'json',
            timeout: 5000,
            onload: async function(response) {
                isFetchingAnonGearCache = false;
                if (response.status === 200) {
                    try {
                        const data = response.response || JSON.parse(response.responseText);
                        anonGearCacheItems = data.listings || [];
                        anonGearCacheLastFetch = new Date();

                        if (!isAnonServerAvailable) {
                            addDebugLog('✓ Anon server is back online!');
                            isAnonServerAvailable = true;
                            anonServerLastCheckTime = new Date();
                            await GM.setValue('isAnonServerAvailable', true);
                            await GM.setValue('anonServerLastCheckTime', anonServerLastCheckTime.toISOString());
                        }

                        addDebugLog(`✓ Fetched ${anonGearCacheItems.length} anon gear cache items`);

                        await GM.setValue('anonGearCacheItems', anonGearCacheItems);
                        await GM.setValue('anonGearCacheLastFetch', anonGearCacheLastFetch.toISOString());

                        updateStatsUI();
                        highlightMatchingItems();
                    } catch (err) {
                        addDebugLog(`✗ Anon gear cache parse error: ${err.message}`);
                        updateStatsUI();
                    }
                } else {
                    addDebugLog(`✗ Anon gear cache fetch failed (HTTP ${response.status}) - marking server as unavailable`);
                    isAnonServerAvailable = false;
                    anonServerLastCheckTime = new Date();
                    await GM.setValue('isAnonServerAvailable', false);
                    await GM.setValue('anonServerLastCheckTime', anonServerLastCheckTime.toISOString());
                    updateStatsUI();
                }
            },
            onerror: async function() {
                isFetchingAnonGearCache = false;

                if (isAnonServerAvailable) {
                    addDebugLog(`⚠️ Anon server unreachable - anonymous items will be ignored until server is back online`);
                }

                isAnonServerAvailable = false;
                anonServerLastCheckTime = new Date();
                await GM.setValue('isAnonServerAvailable', false);
                await GM.setValue('anonServerLastCheckTime', anonServerLastCheckTime.toISOString());

                updateStatsUI();
            },
            ontimeout: async function() {
                isFetchingAnonGearCache = false;

                addDebugLog(`⚠️ Anon server timeout - marking as unavailable`);
                isAnonServerAvailable = false;
                anonServerLastCheckTime = new Date();
                await GM.setValue('isAnonServerAvailable', false);
                await GM.setValue('anonServerLastCheckTime', anonServerLastCheckTime.toISOString());

                updateStatsUI();
            }
        });
    }

    // After a successful send, update ONLY the tile we just tagged — no full-page border wipe +
    // network refetch + rescan (which made every tile flash and lag on each click). The local
    // caches were already updated by the caller, so we just re-run processTile on the matching
    // tile. Matched by name + stats; a scrape-less send (no name) is left to the auto-fetch loop.
    function refreshTileForListing(data) {
        const name = data && (data.item_name || data.itemName);
        if (!name) return;
        const dmg = parseFloat(data.damage_armor != null ? data.damage_armor : (data.stats && data.stats.damage));
        const acc = parseFloat((data.accuracy != null ? data.accuracy : (data.stats && data.stats.accuracy)) || 0);
        document.querySelectorAll(SEL.tile).forEach(tile => {
            const nameEl = tile.querySelector(SEL.name);
            if (!nameEl || nameEl.textContent.trim() !== name) return;
            const pv = tile.querySelectorAll(`${SEL.property} ${SEL.value}`);
            let tDmg = null, tAcc = 0;
            if (pv.length >= 2) {
                tDmg = parseFloat(pv[0].textContent.trim());
                tAcc = parseFloat(pv[1].textContent.trim());
            } else if (pv.length === 1) {
                tDmg = parseFloat(pv[0].textContent.trim());
            }
            if (tDmg === dmg && (tAcc === acc || isNaN(acc))) {
                processTile(tile, name, tDmg, tAcc);
            }
        });
    }

    async function highlightMatchingItems() {
        if (!isScriptEnabled) return;

        // Highlight uses whatever is cached locally; fetching is handled by the auto-fetch loop
        // (every 30s) or the manual Fetch button — not on every DOM mutation.
        const currentUncachedCount = uncachedItems.length;
        const currentGearCacheCount = gearCacheItems.length;
        const currentAnonCacheCount = isAnonServerAvailable ? anonGearCacheItems.length : 'OFFLINE';

        if (currentUncachedCount !== prevUncachedCount ||
            currentGearCacheCount !== prevGearCacheCount ||
            currentAnonCacheCount !== prevAnonCacheCount) {

            addDebugLog(`Cache status - Uncached: ${currentUncachedCount}, Gear: ${currentGearCacheCount}, Anon: ${currentAnonCacheCount}`);

            prevUncachedCount = currentUncachedCount;
            prevGearCacheCount = currentGearCacheCount;
            prevAnonCacheCount = currentAnonCacheCount;
        }

        const itemTiles = document.querySelectorAll(SEL.tile);
        let greenHighlights = 0;

        itemTiles.forEach(tile => {
            const nameEl = tile.querySelector(SEL.name);
            if (!nameEl) return;

            const itemName = nameEl.textContent.trim();
            const propertyValues = tile.querySelectorAll(`${SEL.property} ${SEL.value}`);

            if (propertyValues.length === 0) return;

            let damageArmor = null;
            let accuracy = null;

            if (propertyValues.length === 2) {
                damageArmor = parseFloat(propertyValues[0].textContent.trim());
                accuracy = parseFloat(propertyValues[1].textContent.trim());
            } else if (propertyValues.length === 1) {
                damageArmor = parseFloat(propertyValues[0].textContent.trim());
                accuracy = 0;
            }

            // Re-process every tile each pass (cheap: only a small uncached-list lookup). This
            // keeps borders correct — a tile that just got captured loses its green border, and any
            // stale border from a prior version is cleared.
            processTile(tile, itemName, damageArmor, accuracy);

            if (isGreenTile(tile)) greenHighlights++;
        });

        if (greenHighlights !== prevGreenHighlights) {
            addDebugLog(`Highlighted ${greenHighlights} uncached (green) item(s)`);
            prevGreenHighlights = greenHighlights;
        }
    }

    function processTile(tile, itemName, damageArmor, accuracy) {
        // GREEN = this weapon is on the server's "uncached" list, i.e. it still needs a seller
        // captured. The uncached list is authoritative (server-side, with real API stats), so it's
        // the only reliable signal. Everything else gets NO border. (We used to paint "uncaptured
        // gear" blue by matching the local gear cache on stats, but armouryID-only captures have no
        // stats, so that flagged every already-captured weapon blue.)
        const shouldBeGreen = uncachedItems.some(item => {
            const uncachedItemName = item.item_name || item.itemName;
            if (uncachedItemName !== itemName) return false;
            const itemDamageArmor = parseFloat(item.damage_armor);
            const itemAccuracy = parseFloat(item.accuracy || 0);
            return itemDamageArmor === damageArmor && itemAccuracy === accuracy;
        });

        // IDEMPOTENT: only touch the DOM when this tile's state actually changes. This is what
        // stops the whole page's borders from flashing every time you expand an item — tiles that
        // are already correct are left untouched, so only the weapon you just captured (green ->
        // none) actually redraws.
        const hasGreen = isGreenTile(tile);
        if (shouldBeGreen && !hasGreen) {
            tile.style.border = '2px solid #00ff00';
            tile.style.boxShadow = '0 0 10px rgba(0, 255, 0, 0.5)';
        } else if (!shouldBeGreen && hasGreen) {
            tile.style.border = '';
            tile.style.boxShadow = '';
            const badge = tile.querySelector('.uncached-price-badge');
            if (badge) badge.remove();
        }
    }

    function setupApiInterception() {
        if (!isOnItemMarketPage()) {
            return;
        }

        if (!unsafeWindow.__originalFetch) {
            unsafeWindow.__originalFetch = unsafeWindow.fetch;
        }

        let originalFetch = unsafeWindow.__originalFetch;
        let currentArmouryID = null;

        unsafeWindow.fetch = function(...args) {
            let [url, options] = args;
            let urlString = typeof url === 'string' ? url : (url && url.url ? url.url : '');

            if (urlString.includes('getListing') && isOnItemMarketPage() && isScriptEnabled && options && options.body) {
                // Reset per request — a stale armouryID from a prior gear view must not leak into
                // an item (stackable) view and misclassify it as gear (=> failed gear scrape).
                currentArmouryID = null;
                try {
                    const params = new URLSearchParams(options.body);
                    const armouryID = params.get('armouryID');
                    if (armouryID && String(armouryID) !== "0") {
                        currentArmouryID = armouryID;
                    }
                } catch (err) {
                    addDebugLog(`Error parsing request body: ${err.message}`);
                }
            }

            if (urlString.includes('getListing') && isOnItemMarketPage() && isScriptEnabled) {
                return originalFetch.apply(this, args).then(response => {
                    let clonedResponse = response.clone();
                    clonedResponse.json().then(data => {
                        if (data.list && data.list.length > 0) {
                            // DEBUG: dump the raw getListing response so we can see the exact schema.
                            if (DEBUG_CONSOLE_ONLY) {
                                console.log('[ITEM MATCHER] raw getListing response:\n' + JSON.stringify(data, null, 2));
                            }
                            // Gear listings carry an armouryID (= the API uid); stackable items
                            // don't ("0"/absent).
                            const looksGear = !!(currentArmouryID && String(currentArmouryID) !== "0")
                                || !!(data.list[0].armouryID && String(data.list[0].armouryID) !== "0");
                            if (looksGear) {
                                // Capture EVERY gear listing on the page (a category view returns
                                // many) — not just the first, or the others stay "uncached" forever.
                                // Only the expanded weapon has a stats panel to scrape; the rest are
                                // sent by armouryID alone (the exact server join key).
                                const expandedUid = currentArmouryID;
                                const single = data.list.length === 1;
                                data.list.forEach((listing) => {
                                    if (single && expandedUid && (!listing.armouryID || String(listing.armouryID) === "0")) {
                                        listing.armouryID = expandedUid;
                                    }
                                    const isExpanded = single ||
                                        (!!expandedUid && String(listing.armouryID) === String(expandedUid));
                                    tryAutoSendListing(listing, 20, { scrape: isExpanded });
                                });
                            } else {
                                captureItemListings(data.list);
                            }
                        }
                    }).catch(err => {
                        addDebugLog(`Error parsing getListing response: ${err.message}`);
                    });
                    return response;
                });
            }
            return originalFetch.apply(this, args);
        };
        addDebugLog('API interception setup complete');
    }

    function tryAutoSendListing(listing, retries = 20, opts) {
        if (!isScriptEnabled) return;
        const wantScrape = !opts || opts.scrape !== false;

        const isAnonymous = listing.anonymous === true ||
                           (listing.user && listing.user.ID === "0") ||
                           (listing.user && listing.user.ID === 0);

        // Anonymous gear goes to the same server as everything else now — always attempt it.

        if (isAnonymous) {
            if (anonCacheIDs.has(listing.listingID)) {
                return;
            }
        } else {
            if (gearCacheIDs.has(listing.listingID)) {
                return;
            }
        }

        // Only the expanded weapon has a stats panel; for the others (category view) skip the
        // scrape and send straight away by armouryID.
        let scrapedData = wantScrape ? scrapeItemDetailsFromPage() : null;

        // The DOM stats panel can lag the API response (or break on a Torn redeploy). Retry a few
        // times to enrich with damage/accuracy/bonuses — but if it never shows, SEND ANYWAY using
        // the API data. The server matches gear by armouryID (the exact uid), so a missing stats
        // panel must not cause us to silently drop the seller (which left the item "uncached").
        if (wantScrape && !scrapedData && retries > 0) {
            setTimeout(() => tryAutoSendListing(listing, retries - 1, opts), 100);
            return;
        }

        const listerId = isAnonymous ? "0" : ((listing.user && listing.user.ID) ? listing.user.ID : "0");
        const sellerName = isAnonymous ? "Anonymous"
            : ((listing.user && listing.user.name) ? listing.user.name : "Anonymous");

        // item_id straight from the API (relatedItemID); fall back to the scraped name mapping.
        const itemId = listing.relatedItemID
            || (scrapedData ? (ITEM_ID_MAP[scrapedData.item_name] || null) : null);

        const listingData = {
            listingID: listing.listingID,
            item_id: itemId,
            item_name: scrapedData ? scrapedData.item_name : null,
            damage_armor: scrapedData ? scrapedData.damage_armor : null,
            accuracy: scrapedData ? scrapedData.accuracy : null,
            bonuses: scrapedData ? scrapedData.bonuses : [],
            lister: listerId,
            seller_name: sellerName,
            price: listing.price,
            // armouryID IS the API item_details.uid (confirmed) — the exact gear join key.
            armouryID: listing.armouryID,
            adder: 'auto-scraper'
        };

        const label = listingData.item_name || (itemId ? `item ${itemId}` : 'gear');
        if (!scrapedData) {
            addDebugLog(`⚠️ No stats panel — sending ${label} by armouryID only`);
        }

        if (isAnonymous) {
            const isOnUncachedList = uncachedItems.some(item => {
                const nameMatch = (item.item_name || item.itemName) === listingData.item_name;
                if (!nameMatch) return false;

                const itemDamageArmor = parseFloat(item.damage_armor || item.stats?.damage || 0);
                const itemAccuracy = parseFloat(item.accuracy || item.stats?.accuracy || 0);
                const listingDamageArmor = parseFloat(listingData.damage_armor);
                const listingAccuracy = parseFloat(listingData.accuracy || 0);

                return itemDamageArmor === listingDamageArmor && itemAccuracy === listingAccuracy;
            });

            anonCacheIDs.add(listing.listingID);
            anonListingQueue.push(listingData);
            addDebugLog(`Queued ANON: ${label} ($${listingData.price})`);
            processAnonQueue();

            if (isOnUncachedList) {
                gearCacheIDs.add(listing.listingID);
                listingQueue.push(listingData);
                addDebugLog(`Also queued for MAIN (uncached): ${label}`);
            }
        } else {
            gearCacheIDs.add(listing.listingID);
            listingQueue.push(listingData);
            addDebugLog(`Queued: ${label} ($${listingData.price})`);
        }

        updateStatsUI();
    }

    function scrapeItemDetailsFromPage() {
        try {
            let itemName = document.body.querySelector(`${SEL.expanded} ${SEL.name}`)?.textContent?.trim();
            if (!itemName) return null;

            let damageArmor;
            let valueElement = document.body.querySelector(`${SEL.expanded} ${SEL.value}`);

            if (valueElement) {
                damageArmor = valueElement.textContent.trim();
            } else {
                return null;
            }

            let accuracy = '0.0';
            let propertyValues = document.body.querySelectorAll(`${SEL.expanded} ${SEL.properties} ${SEL.value}`);
            if (propertyValues.length >= 2) {
                accuracy = propertyValues[1].textContent.trim();
            }

            let bonuses = [];
            let bonusElements = document.body.querySelectorAll(`${SEL.expanded} ${SEL.bonuses} ${SEL.icon}`);
            bonusElements.forEach(bonusEl => {
                let description = bonusEl.getAttribute('data-bonus-attachment-description');
                let name = bonusEl.getAttribute('data-bonus-attachment-title') || bonusEl.getAttribute('aria-label') || 'Unknown';
                if (description) {
                    let match = description.match(/(\d+)%/) || description.match(/(\d+) turns?/);
                    if (match) {
                        let value = match[1];
                        let isPercentage = description.includes('%');
                        bonuses.push({
                            name: name,
                            description: description,
                            value: value,
                            isPercentage: isPercentage
                        });
                    }
                }
            });

            return { item_name: itemName, damage_armor: damageArmor, accuracy: accuracy, bonuses: bonuses };
        } catch (err) {
            addDebugLog(`Scrape error: ${err.message}`);
            return null;
        }
    }

    // Stackable items: capture every non-anonymous listing on the viewed page (seller + price +
    // quantity). The server matches these to the API poll by price+quantity to detect sales.
    function captureItemListings(list) {
        if (!isScriptEnabled) return;
        const payload = [];
        for (const l of list) {
            const isAnon = l.anonymous === true || !l.user || l.user.ID === "0" || l.user.ID === 0;
            // Send anonymous listings too (flagged). They can't be mugged, but they still occupy a
            // market slot — the server counts them as "accounted for" so they stop inflating the
            // item's uncaptured count (which is why anon listings looked permanently "missing").
            payload.push({
                listingID: l.listingID,
                item_id: l.relatedItemID,
                seller_id: isAnon ? null : l.user.ID,
                seller_name: isAnon ? null : l.user.name,
                anonymous: isAnon,
                price: l.price,
                available: l.available
            });
        }
        if (payload.length === 0) return;
        if (DEBUG_CONSOLE_ONLY) {
            console.log('[ITEM MATCHER] WOULD POST /items (' + payload.length + '):\n' + JSON.stringify(payload, null, 2));
            return;
        }
        vlog('→ POST /items (' + payload.length + '): ' + JSON.stringify(payload));
        GM[httpRequest]({
            method: 'POST',
            url: `${serverUrl()}/items`,
            data: JSON.stringify({ listings: payload }),
            headers: { 'Content-Type': 'application/json' },
            onload: (r) => { markMainServer(r.status === 200, `HTTP ${r.status}`);
                             addDebugLog(`✓ Sent ${payload.length} item listings`); vlog('← /items ' + r.status); },
            onerror: () => { markMainServer(false, 'network error');
                             addDebugLog(`✗ Error sending item listings`); }
        });
    }

    function sendCompleteListingData(listingData) {
        if (DEBUG_CONSOLE_ONLY) {
            console.log('[ITEM MATCHER] WOULD POST /gear:\n' + JSON.stringify(listingData, null, 2));
            return;
        }
        vlog('→ POST /gear: ' + JSON.stringify(listingData));
        GM[httpRequest]({
            method: 'POST',
            url: MAIN_SUBMIT_URL(),
            data: JSON.stringify(listingData),
            headers: { 'Content-Type': 'application/json' },
            responseType: 'json',
            onload: function(response) {
                if (response.status === 200) {
                    itemsSentCount++;
                    markMainServer(true);
                    addDebugLog(`✓ Sent: ${listingData.item_name} ($${listingData.price})`);

                    // NOTE: we do NOT optimistically remove the item from the uncached list here. A
                    // 200 only means the server RECEIVED the capture — not that it verified the uid
                    // joined to that uncached weapon. The item stays on the list (tile stays green)
                    // until the next /cache/uncached fetch confirms the server actually matched it.
                    const newCacheItem = {
                        listingID: listingData.listingID,
                        itemId: null,
                        itemName: listingData.item_name,
                        price: listingData.price,
                        available: 1,
                        user: { ID: listingData.lister, name: listingData.seller_name },
                        type: 'gear',
                        bonuses: listingData.bonuses || [],
                        armouryID: listingData.armouryID,
                        stats: {
                            damage: parseFloat(listingData.damage_armor),
                            accuracy: parseFloat(listingData.accuracy || 0)
                        }
                    };

                    gearCacheItems.push(newCacheItem);
                    GM.setValue('gearCacheItems', gearCacheItems);

                    updateStatsUI();

                    refreshTileForListing(listingData);
                } else {
                    markMainServer(false, `HTTP ${response.status}`);
                    addDebugLog(`✗ Failed to send (HTTP ${response.status}): ${listingData.item_name}`);
                }
            },
            onerror: function() {
                markMainServer(false, 'network error');
                addDebugLog(`✗ Network error sending: ${listingData.item_name}`);
            }
        });
    }

    function sendAnonListingData(listingData) {
        const normalizedData = {
            listingID: listingData.listingID,
            itemId: null,
            itemName: listingData.item_name,
            price: listingData.price,
            available: 1,
            user: { ID: "0", name: "Anonymous" },
            type: 'gear',
            bonuses: listingData.bonuses || [],
            armouryID: listingData.armouryID,
            stats: {
                damage: listingData.damage_armor,
                accuracy: listingData.accuracy
            }
        };

        if (DEBUG_CONSOLE_ONLY) {
            console.log('[ITEM MATCHER] WOULD POST /gear/anon:\n' + JSON.stringify(normalizedData, null, 2));
            return;
        }
        GM[httpRequest]({
            method: 'POST',
            url: ANON_SUBMIT_URL(),
            data: JSON.stringify(normalizedData),
            headers: { 'Content-Type': 'application/json' },
            responseType: 'json',
            onload: function(response) {
                if (response.status === 200) {
                    anonItemsSentCount++;
                    addDebugLog(`✓ Sent ANON: ${normalizedData.itemName} ($${normalizedData.price})`);

                    anonGearCacheItems.push(normalizedData);
                    GM.setValue('anonGearCacheItems', anonGearCacheItems);

                    updateStatsUI();

                    refreshTileForListing(normalizedData);
                } else {
                    addDebugLog(`✗ Failed to send ANON (HTTP ${response.status}): ${normalizedData.itemName}`);
                }
            },
            onerror: function() {
                addDebugLog(`✗ Network error sending ANON (is local server running?): ${normalizedData.itemName}`);
            }
        });
    }

    function processListingQueue() {
        // Drain the whole queue each tick (gear listings go 1 POST at a time, but with no
        // artificial throttle — just buffered through the queue as they're captured). Only
        // re-render when we actually sent something — otherwise this 500ms timer would rebuild
        // the panel every tick and reset the log's scroll position (even while disabled).
        let drained = false;
        while (listingQueue.length > 0 && isScriptEnabled) {
            sendCompleteListingData(listingQueue.shift());
            drained = true;
        }
        if (drained) updateStatsUI();
    }

    function processAnonQueue() {
        while (anonListingQueue.length > 0 && isScriptEnabled) {
            const itemToSend = anonListingQueue.shift();
            sendAnonListingData(itemToSend);
            updateStatsUI();
        }
    }

    function createStatsUI() {
        if (statsUI) return;

        statsUI = document.createElement('div');
        statsUI.id = 'item-matcher-stats';
        statsUI.style.cssText = `
            position: fixed;
            top: 10px;
            right: 10px;
            background: rgba(0, 0, 0, 0.9);
            color: #fff;
            padding: 12px;
            border-radius: 8px;
            font-family: monospace;
            font-size: 12px;
            z-index: 999990;
            min-width: 300px;
            max-width: 500px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
            border: 1px solid #333;
        `;

        let isDragging = false;
        let currentX, currentY, initialX, initialY;

        statsUI.addEventListener('mousedown', (e) => {
            if (e.target.tagName === 'BUTTON' ||
                e.target.id.includes('-toggle') ||
                e.target.id.includes('-sort-') ||
                e.target.tagName === 'A' ||
                e.target.tagName === 'INPUT' ||
                e.target.closest('#debug-log-content')) {
                return;
            }
            isDragging = true;
            initialX = e.clientX - statsUI.offsetLeft;
            initialY = e.clientY - statsUI.offsetTop;
        });

        document.addEventListener('mousemove', (e) => {
            if (isDragging) {
                e.preventDefault();
                currentX = e.clientX - initialX;
                currentY = e.clientY - initialY;
                statsUI.style.left = currentX + 'px';
                statsUI.style.top = currentY + 'px';
                statsUI.style.right = 'auto';
            }
        });

        document.addEventListener('mouseup', () => {
            isDragging = false;
        });

        document.body.appendChild(statsUI);
        updateStatsUI();
    }

    function generateItemSearchURL(item) {
        const itemID = item.item_id || ITEM_ID_MAP[item.item_name];
        if (!itemID) return null;

        // Stackable item (no stats): link to its category so viewing it loads the listings, which
        // the script captures and sends back. e.g. categoryName=Energy%20Drink&itemID=533.
        if (item.damage_armor === undefined || item.damage_armor === null) {
            if (item.category) {
                return `https://www.torn.com/page.php?sid=ItemMarket#/market/view=category&categoryName=${encodeURIComponent(item.category)}&itemID=${itemID}`;
            }
            return `https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID=${itemID}&sortField=price&sortOrder=ASC`;
        }

        const fromValue = Math.floor(item.damage_armor);
        const toValue = fromValue + 1;
        const fromAccuracy = Math.floor(item.accuracy || 0);
        const toAccuracy = fromAccuracy + 1;

        let priceParams = '';
        const price = item.price ? parseInt(item.price, 10) : 0;

        if (PRICE_FILTER_PERCENTAGE !== -1 && price > 0) {
            if (PRICE_FILTER_PERCENTAGE === 0) {
                priceParams = `&priceFrom=${price}&priceTo=${price}`;
            } else {
                const priceVariation = Math.floor(price * (PRICE_FILTER_PERCENTAGE / 100));
                const priceFrom = price - priceVariation;
                const priceTo = price + priceVariation;
                priceParams = `&priceFrom=${priceFrom}&priceTo=${priceTo}`;
            }
        }

        let searchURL;
        if (ARMOR_NAMES.has(item.item_name)) {
            searchURL = `https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID=${itemID}&sortField=price&sortOrder=DESC&armorFrom=${fromValue}&armorTo=${toValue}${priceParams}`;
        } else {
            searchURL = `https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID=${itemID}&sortField=price&sortOrder=DESC&damageFrom=${fromValue}&damageTo=${toValue}&accuracyFrom=${fromAccuracy}&accuracyTo=${toAccuracy}${priceParams}`;
        }
        return searchURL;
    }

    // Single source of truth for the uncached-list ordering (price/name, asc/desc), shared by the
    // list render, the w/s keyboard nav, and the Easy walker so they always agree.
    function sortedUncachedItems() {
        const items = [...uncachedItems];
        if (uncachedSortBy === 'price') {
            items.sort((a, b) => {
                const ap = (typeof a.price === 'number') ? a.price : 0;
                const bp = (typeof b.price === 'number') ? b.price : 0;
                return uncachedSortOrder === 'asc' ? ap - bp : bp - ap;
            });
        } else {
            items.sort((a, b) => uncachedSortOrder === 'asc'
                ? a.item_name.localeCompare(b.item_name)
                : b.item_name.localeCompare(a.item_name));
        }
        return items;
    }

    // Open the matched green (uncached) tile's listing so the seller is revealed + captured. The
    // seller-reveal control is the "Buy Item" cart button: in Torn's market it EXPANDS the listing's
    // panel (showing the seller + firing getListing) — it does NOT buy (that needs a separate confirm
    // inside the expanded panel, which we never click). Scoped to the matched green tile only.
    function openTargetTile(item) {
        const tiles = [...document.querySelectorAll(SEL.tile)];
        const greens = tiles.filter(isGreenTile);
        let target = item && greens.find(t => {
            const n = t.querySelector(SEL.name);
            return n && n.textContent.trim() === item.item_name;
        });
        if (!target) target = greens[0];
        if (!target && item) target = tiles.find(t => {
            const n = t.querySelector(SEL.name);
            return n && n.textContent.trim() === item.item_name;
        });
        console.log(`[ITEM MATCHER] Easy.open: ${tiles.length} tiles, ${greens.length} green, target=${target ? target.querySelector(SEL.name)?.textContent.trim() : 'NONE'}`);
        if (!target) return false;

        const reveal = (el) => el && ['pointerover', 'mouseover', 'mouseenter'].forEach(t =>
            el.dispatchEvent(new MouseEvent(t, { bubbles: true })));
        reveal(target);
        reveal(target.querySelector('[class*="actionsWrapper"]'));
        target.scrollIntoView({ block: 'center' });

        const buy = target.querySelector('button[aria-label^="Buy item" i]');
        if (!buy) { console.log('[ITEM MATCHER] Easy.open: no Buy button in tile:\n' + target.outerHTML); return false; }
        // Only click while collapsed, so a stray double-press can never reach a confirm inside the
        // already-open panel. Never touches View Info (the item encyclopedia).
        if (buy.getAttribute('aria-expanded') === 'true') {
            console.log('[ITEM MATCHER] Easy.open: listing already expanded — not re-clicking');
            return true;
        }
        reveal(buy);
        buy.click();
        console.log('[ITEM MATCHER] Easy.open: clicked Buy (expand) →', item && item.item_name);
        return true;
    }

    // Two presses per item: navigate to the uncached weapon's page, then open its listing to reveal
    // + capture the seller. Advancing to the next weapon is a plain index step taken on the OPEN
    // press. (We used to conditionally advance by comparing the last-opened item's signature — needed
    // only while the script optimistically removed items on send, which shifted the list under us.
    // Now items leave the list only on the next server fetch, so the simple step is reliable and no
    // longer needs a second press.) Hotkey: E. Wraps around; the ~30s fetch clears captured weapons.
    function easyButtonStep() {
        if (!isScriptEnabled) { addDebugLog('⚡ Easy: script is OFF'); return; }
        const items = sortedUncachedItems();
        if (items.length === 0) {
            addDebugLog('⚡ Easy: nothing left to capture');
            easyIndex = 0; easyPhase = 'navigate'; easyNavItem = null;
            updateStatsUI();
            return;
        }
        if (easyPhase === 'navigate') {
            if (easyIndex >= items.length) easyIndex = 0;
            const item = items[easyIndex];
            easyNavItem = item;
            selectedSortedItemIndex = easyIndex;
            const url = generateItemSearchURL(item);
            addDebugLog(`⚡ Easy: → ${item.item_name} (${easyIndex + 1}/${items.length}) — press again to open seller`);
            easyPhase = 'open';
            updateStatsUI();
            if (url) window.location.href = url; else addDebugLog(`⚡ Easy: no URL for ${item.item_name}`);
        } else {
            // Open the item we NAVIGATED to (not items[easyIndex], which may have shifted) — the green
            // tile is matched by name on the page.
            const item = easyNavItem || items[Math.min(easyIndex, items.length - 1)];
            const opened = openTargetTile(item);
            addDebugLog(opened
                ? `⚡ Easy: opened ${item.item_name} — seller revealed/captured`
                : `⚡ Easy: no green tile for ${item.item_name} (already captured?)`);
            easyIndex = (easyIndex + 1) % items.length;   // step to the next weapon
            easyPhase = 'navigate';
            updateStatsUI();
        }
    }

    function handleListNavigation(e) {
        if (e.key !== 'w' && e.key !== 's' && e.key !== ' ' && e.key !== 'e') return;

        if (document.activeElement && (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA')) {
            return;
        }

        // Easy walker — works whether or not the uncached-list panel is expanded.
        if (e.key === 'e') {
            e.preventDefault();
            easyButtonStep();
            return;
        }

        if (!isUncachedListVisible || uncachedItems.length === 0) return;

        e.preventDefault();

        const maxIndex = uncachedItems.length - 1;

        if (e.key === 's') {
            selectedSortedItemIndex++;
            if (selectedSortedItemIndex > maxIndex) selectedSortedItemIndex = 0;
        } else if (e.key === 'w') {
            selectedSortedItemIndex--;
            if (selectedSortedItemIndex < 0) selectedSortedItemIndex = maxIndex;
        } else if (e.key === ' ') {
            selectedSortedItemIndex = 0;
        }

        const sortedItems = sortedUncachedItems();

        const item = sortedItems[selectedSortedItemIndex];
        if (!item) return;

        const url = generateItemSearchURL(item);

        updateStatsUI();
        if (url) {
            window.location.href = url;
        }
    }

    function updateStatsUI() {
        if (!statsUI) return;

        const statusColor = isScriptEnabled ? '#66ff66' : '#ff6666';
        const statusText = isScriptEnabled ? 'ACTIVE' : 'DISABLED';
        const queueSize = listingQueue.length;
        const anonQueueSize = anonListingQueue.length;

        const fetchButtonColor = isFetching ? '#888' : '#4488ff';
        const fetchButtonText = isFetching ? 'Fetching...' : '🔄 Fetch';
        const lastFetchText = lastFetchTime
            ? `Last: ${lastFetchTime.toLocaleTimeString()}`
            : 'Never fetched';

        const uncachedToggleText = isUncachedListVisible ? 'Hide' : 'Show';
        let uncachedListHTML = '';

        if (isUncachedListVisible) {
            const priceArrow = uncachedSortBy === 'price' ? (uncachedSortOrder === 'asc' ? ' ▲' : ' ▼') : '';
            const nameArrow = uncachedSortBy === 'name' ? (uncachedSortOrder === 'asc' ? ' ▲' : ' ▼') : '';

            const sortPriceColor = uncachedSortBy === 'price' ? '#66ff66' : '#888';
            const sortPriceWeight = uncachedSortBy === 'price' ? 'bold' : 'normal';
            const sortNameColor = uncachedSortBy === 'name' ? '#66ff66' : '#888';
            const sortNameWeight = uncachedSortBy === 'name' ? 'bold' : 'normal';

            const sortedItems = sortedUncachedItems();

            const itemsHTML = sortedItems.map((item, index) => {
                const searchURL = generateItemSearchURL(item);
                let nameHTML = '';

                if (searchURL) {
                    nameHTML = `<a href="${searchURL}" style="color: #66aaff; text-decoration: none; white-space: normal; word-break: break-all; padding-right: 10px;">${item.item_name}</a>`;
                } else {
                    nameHTML = `<span style="white-space: normal; word-break: break-all; padding-right: 10px;">${item.item_name}</span>`;
                }

                const isSelected = (index === selectedSortedItemIndex);
                const selectionStyle = isSelected ? 'background-color: #223355; outline: 1px solid #4488ff;' : '';

                // Gear has a price; stackable items show how many listings still need a seller.
                let rightHTML;
                if (typeof item.price === 'number') {
                    rightHTML = `<strong style="color: #66ff66; white-space: nowrap;">$${item.price.toLocaleString()}</strong>`;
                } else if (typeof item.missing === 'number') {
                    rightHTML = `<span style="color: #ffcc44; white-space: nowrap;">${item.missing} to capture</span>`;
                } else {
                    rightHTML = `<span style="color: #888; white-space: nowrap;">${item.category || 'item'}</span>`;
                }

                return `
                    <div style="font-size: 11px; color: #ccc; margin-top: 4px; display: flex; justify-content: space-between; border-bottom: 1px solid #222; padding: 4px; ${selectionStyle}">
                        ${nameHTML}
                        ${rightHTML}
                    </div>
                `;
            }).join('');

            uncachedListHTML = `
                <div style="margin-top: 8px;">
                    <div style="font-size: 10px; color: #888; margin-bottom: 5px;">
                        Sort by:
                        <button id="im-uncached-sort-price" style="background: none; border: none; color: ${sortPriceColor}; cursor: pointer; font-size: 10px; font-weight: ${sortPriceWeight}; padding: 2px 4px;">Price${priceArrow}</button>
                        <button id="im-uncached-sort-name" style="background: none; border: none; color: ${sortNameColor}; cursor: pointer; font-size: 10px; font-weight: ${sortNameWeight}; padding: 2px 4px;">Name${nameArrow}</button>
                    </div>
                    <div id="im-uncached-scroll" style="max-height: 300px; overflow-y: scroll; border: 1px solid #333; padding: 5px 8px; border-radius: 4px; background: #111;">
                        ${sortedItems.length > 0 ? itemsHTML : '<div style="font-size: 11px; color: #888;">No uncached items found.</div>'}
                    </div>
                </div>
            `;
        }

        const priceFilterStatus = PRICE_FILTER_PERCENTAGE === -1 ?
            '<span style="color: #ff6666;">DISABLED</span>' :
            `<span style="color: #66ff66;">${PRICE_FILTER_PERCENTAGE}%</span>`;

        let settingsContentHTML = '';
        if (isSettingsVisible) {
            settingsContentHTML = `
                <div style="margin-top: 8px; margin-bottom: 8px; border: 1px solid #333; padding: 8px; border-radius: 4px; background: #111;">
                    <div style="font-size: 10px; color: #aaa; margin-bottom: 6px;">
                        Price Filter: Adds ±X% price range to uncached item search URLs
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <label style="font-size: 11px; color: #888;">Percentage:</label>
                        <input
                            id="im-price-filter-input"
                            type="number"
                            value="${PRICE_FILTER_PERCENTAGE}"
                            min="-1"
                            max="100"
                            style="
                                width: 60px;
                                background: #222;
                                border: 1px solid #444;
                                color: #fff;
                                padding: 4px 6px;
                                border-radius: 4px;
                                font-size: 11px;
                            "
                        />
                        <button id="im-price-filter-apply" style="
                            background: #4488ff;
                            color: #fff;
                            border: none;
                            padding: 4px 8px;
                            border-radius: 4px;
                            cursor: pointer;
                            font-size: 10px;
                            font-weight: bold;
                        ">Apply</button>
                    </div>
                    <div style="font-size: 9px; color: #666; margin-top: 4px;">
                        Use -1 to disable, or 0-100 for percentage range
                    </div>
                </div>
            `;
        }

        let debugLogsHTML = '';
        if (isDebugLogVisible) {
            if (debugLog.length > 0) {
                debugLogsHTML = debugLog.slice(0, 20).map(log =>
                    `<div style="font-size: 10px; color: #aaa; margin-top: 2px; white-space: normal; word-break: break-all;">${log}</div>`
                ).join('');
            } else {
                debugLogsHTML = `<div style="font-size: 10px; color: #666;">No activity logged yet.</div>`;
            }
            debugLogsHTML = `<div id="debug-log-content" style="max-height: 150px; overflow-y: auto; padding: 8px; margin-top: 8px; border: 1px solid #333; border-radius: 4px; background: #111; user-select: text; cursor: text;">${debugLogsHTML}</div>`;
        }

        const controlsSection = `
            <div style="border-top: 1px solid #444; margin-top: 8px; padding-top: 8px;">
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 10px;">
                    <div>
                        <span style="color: #888;">Price Filter: </span>
                        <span id="im-price-filter-toggle" style="cursor: pointer; text-decoration: underline;">${priceFilterStatus}</span>
                    </div>
                    <div>
                        <span id="im-log-toggle" style="color: #4488ff; cursor: pointer; text-decoration: underline;">Log</span>
                    </div>
                </div>
                <div style="font-size: 9px; margin-top: 4px;">
                    <span style="color: #666;">Server: </span>
                    <span id="im-server-url" title="Click to change (shared with market-tracker)" style="
                        color: ${isMainServerAvailable ? '#888' : '#ff6666'};
                        cursor: pointer; text-decoration: underline; word-break: break-all;
                    ">${serverUrl().replace(/^https?:\/\//, '')}</span>
                </div>
                ${settingsContentHTML}
                ${debugLogsHTML}
            </div>
        `;

        if (isMinimized) {
            statsUI.innerHTML = `
                <div id="im-minimized" style="
                    cursor: pointer;
                    padding: 4px 8px;
                    color: #fff;
                    font-weight: bold;
                    font-size: 12px;
                ">
                    Item Matcher — minimized (click to expand)
                </div>
            `;
            document.getElementById('im-minimized').addEventListener('click', toggleMinimize);
            return;
        }

        // Rebuilding innerHTML resets scroll; remember the log + uncached scroll positions so the
        // panel doesn't snap to the top while you're reading/scrolling.
        const _prevLogScroll = document.getElementById('debug-log-content')?.scrollTop;
        const _prevUncachedScroll = document.getElementById('im-uncached-scroll')?.scrollTop;

        // An unreachable server is the one failure that used to be invisible — captures kept being
        // queued and thrown away with a single line in a log panel nobody opens. It gets the top of
        // the panel, in red, above everything else.
        const serverBanner = isMainServerAvailable ? '' : `
            <div style="background: #4a1111; border: 1px solid #ff4444; border-radius: 4px;
                        padding: 6px; margin-bottom: 8px; font-size: 11px; color: #ff9999;">
                <div style="font-weight: bold; color: #ff6666;">\u26a0 SERVER UNREACHABLE</div>
                <div style="margin-top: 2px; word-break: break-all;">${serverUrl()}</div>
                <div style="margin-top: 2px; color: #cc8888;">Nothing is being captured. Click the
                    address below to change it.</div>
            </div>
        `;

        statsUI.innerHTML = `
            ${serverBanner}
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div style="font-weight: bold; color: ${statusColor};">Item Matcher ${statusText}</div>

                <div>
                    <button id="im-minimize-btn" style="
                        background: #444;
                        color: #fff;
                        border: none;
                        padding: 4px 6px;
                        border-radius: 4px;
                        cursor: pointer;
                        font-size: 10px;
                        margin-right: 4px;
                    ">–</button>

                    <button id="im-toggle-btn" style="
                        background: ${statusColor};
                        color: #000;
                        border: none;
                        padding: 4px 8px;
                        border-radius: 4px;
                        cursor: pointer;
                        font-size: 10px;
                        font-weight: bold;
                    ">${isScriptEnabled ? 'ON' : 'OFF'}</button>
                </div>
            </div>

            <div style="border-top: 1px solid #444; padding-top: 8px;">
                <div>📊 Sent: <span style="color: #66ff66; font-weight: bold;">${itemsSentCount + anonItemsSentCount}</span></div>
                <div>📦 Queue: <span style="color: ${(queueSize + anonQueueSize) > 0 ? '#ffaa66' : '#888'}; font-weight: bold;">${queueSize + anonQueueSize}</span></div>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>🔧 Gear Cache: <span style="color: #0088ff; font-weight: bold;">${gearCacheItems.length}</span></div>
                    <button id="im-refresh-gear-btn" style="
                        background: ${isFetchingGearCache ? '#888' : '#0088ff'};
                        color: #fff;
                        border: none;
                        padding: 2px 6px;
                        border-radius: 4px;
                        cursor: ${isFetchingGearCache ? 'not-allowed' : 'pointer'};
                        font-size: 9px;
                        font-weight: bold;
                    " ${isFetchingGearCache ? 'disabled' : ''}>${isFetchingGearCache ? '...' : '🔄'}</button>
                </div>
                <div style="color: #666; font-size: 9px; margin-top: 2px;">
                    ${gearCacheLastFetch ? `Last: ${gearCacheLastFetch.toLocaleTimeString()}` : 'Never fetched'}
                </div>
            </div>

            <div style="border-top: 1px solid #444; margin-top: 8px; padding-top: 8px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <div id="im-uncached-toggle" style="color: #888; font-size: 10px; cursor: pointer; font-weight: bold; user-select: none;">
                        Uncached Items: <span style="color: #66ff66; font-weight: bold;">${uncachedItems.length}</span>
                        <span style="color: #4488ff;">[${uncachedToggleText}]</span>
                    </div>
                    <button id="im-autofetch-btn" title="Auto-fetch the list every 30s" style="
                        background: ${autoFetchEnabled ? '#2a7' : '#555'};
                        color: #fff;
                        border: none;
                        padding: 4px 8px;
                        border-radius: 4px;
                        cursor: pointer;
                        font-size: 10px;
                        font-weight: bold;
                        margin-left: auto;
                        margin-right: 4px;
                    ">Auto ${autoFetchEnabled ? 'ON' : 'OFF'}</button>
                    <button id="im-fetch-btn" style="
                        background: ${fetchButtonColor};
                        color: #fff;
                        border: none;
                        padding: 4px 8px;
                        border-radius: 4px;
                        cursor: ${isFetching ? 'not-allowed' : 'pointer'};
                        font-size: 10px;
                        font-weight: bold;
                    " ${isFetching ? 'disabled' : ''}>${fetchButtonText}</button>
                </div>
                <div style="color: #666; font-size: 9px;">${lastFetchText}</div>
                <div style="color: #888; font-size: 9px; margin-top: 2px;">
                    Tile colours: <span style="color:#00ff00;">■</span> needs a seller (uncached) ·
                    no border = captured / not needed
                </div>
                <button id="im-easy-btn" title="Capture walker (hotkey: E). Press 1: go to the next uncached weapon's page. Press 2: expand its green listing (clicks Buy to reveal the seller — does NOT purchase). Then it advances." style="
                    width: 100%;
                    margin-top: 6px;
                    background: ${easyPhase === 'navigate' ? '#7a3cff' : '#ff8c1a'};
                    color: #fff;
                    border: none;
                    padding: 6px 8px;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 11px;
                    font-weight: bold;
                ">⚡ Easy [E] — ${easyPhase === 'navigate' ? '▸ go to next weapon' : '● open seller'}</button>
                ${uncachedListHTML}
            </div>

            ${controlsSection}
        `;

        document.getElementById('im-toggle-btn')?.addEventListener('click', toggleScript);
        document.getElementById('im-minimize-btn')?.addEventListener('click', toggleMinimize);
        document.getElementById('im-fetch-btn')?.addEventListener('click', fetchAllCaches);
        document.getElementById('im-easy-btn')?.addEventListener('click', easyButtonStep);
        document.getElementById('im-autofetch-btn')?.addEventListener('click', toggleAutoFetch);
        document.getElementById('im-refresh-gear-btn')?.addEventListener('click', () => fetchGearCache(true));
        document.getElementById('im-refresh-anon-btn')?.addEventListener('click', () => fetchAnonGearCache(true));
        document.getElementById('im-uncached-toggle')?.addEventListener('click', toggleUncachedList);
        document.getElementById('im-price-filter-toggle')?.addEventListener('click', toggleSettings);
        document.getElementById('im-log-toggle')?.addEventListener('click', toggleDebugLog);
        document.getElementById('im-server-url')?.addEventListener('click', promptForServerUrl);
        document.getElementById('im-uncached-sort-price')?.addEventListener('click', () => setUncachedSort('price'));
        document.getElementById('im-uncached-sort-name')?.addEventListener('click', () => setUncachedSort('name'));

        const priceFilterInput = document.getElementById('im-price-filter-input');
        const priceFilterApply = document.getElementById('im-price-filter-apply');

        if (priceFilterApply) {
            priceFilterApply.addEventListener('click', () => {
                if (priceFilterInput) {
                    updatePriceFilter(priceFilterInput.value);
                }
            });
        }

        if (priceFilterInput) {
            priceFilterInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    updatePriceFilter(priceFilterInput.value);
                }
            });
        }

        // Restore the scroll positions captured before the rebuild.
        if (_prevLogScroll != null) {
            const logEl = document.getElementById('debug-log-content');
            if (logEl) logEl.scrollTop = _prevLogScroll;
        }
        if (_prevUncachedScroll != null) {
            const unEl = document.getElementById('im-uncached-scroll');
            if (unEl) unEl.scrollTop = _prevUncachedScroll;
        }
    }

    loadScriptState().then(() => {
        setupApiInterception();

        GM.registerMenuCommand('Set mug-bot server URL', promptForServerUrl);
        GM.registerMenuCommand('Toggle Item Matcher', toggleScript);
        GM.registerMenuCommand('Fetch Uncached Items', fetchUncachedItems);
        GM.registerMenuCommand('Refresh Gear Cache', () => fetchGearCache(true));
        GM.registerMenuCommand('Refresh Anon Cache', () => fetchAnonGearCache(true));

        function startDomTasks() {
            if (!document.body) {
                setTimeout(startDomTasks, 100);
                return;
            }

            if (!isOnItemMarketPage()) {
                return;
            }

            createStatsUI();

            document.addEventListener('keydown', handleListNavigation);

            // Initial cache pull + start the throttled (30s) auto-fetch loop, then highlight.
            if (isScriptEnabled) {
                fetchAllCaches().then(highlightMatchingItems);
                startAutoFetch();
            }

            setInterval(processListingQueue, SEND_QUEUE_INTERVAL);

            let currentUrl = window.location.href;
            let marketObserver;

            function onMarketMutations() {
                const newUrl = window.location.href;

                if (newUrl !== currentUrl) {
                    currentUrl = newUrl;
                    processedListingIDs.clear();
                    addDebugLog('Page navigation detected - cleared processed items');
                }

                // When disabled, do nothing at all — no highlight, no fetch, no log churn.
                if (!isScriptEnabled || !isOnItemMarketPage()) {
                    return;
                }

                // Re-apply SYNCHRONOUSLY. The observer callback runs as a microtask AFTER Torn's
                // mutations but BEFORE the browser paints, so any borders Torn wiped while
                // expanding a listing are restored in the same frame — no visible "all borders
                // drop then return". (The old 300ms debounce ran on a timer, i.e. after paint,
                // which is exactly what made them flash.) processTile is idempotent, so tiles that
                // are already correct aren't touched. We disconnect around our own writes so we
                // don't re-trigger ourselves.
                marketObserver.disconnect();
                try {
                    highlightMatchingItems();
                } catch (e) {
                    addDebugLog(`Highlight error: ${e.message}`);
                }
                marketObserver.observe(document.body, { childList: true, subtree: true });
            }

            marketObserver = new MutationObserver(onMarketMutations);
            marketObserver.observe(document.body, { childList: true, subtree: true });

            addDebugLog('DOM tasks initialized');
        }

        startDomTasks();
    });

})();