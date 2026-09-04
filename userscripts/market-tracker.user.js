// ==UserScript==
// @name         Market Tracker for Mug Bot
// @version      5.5
// @description  Track market updates and forward to mug bot server
// @author       MonChoon
// @match        https://www.torn.com/page.php?sid=ItemMarket*
// @run-at       document-end
// @icon         https://www.google.com/s2/favicons?sz=64&domain/torn.com
// @grant        GM.xmlHttpRequest
// @grant        GM.registerMenuCommand
// @grant        GM.getValue
// @grant        GM.setValue
// @connect      51.68.212.98
// @require      https://cdn.jsdelivr.net/npm/centrifuge@6.2.5/dist/centrifuge.min.js
// ==/UserScript==

(function() {
    'use strict';

    console.log('[MARKET TRACKER] Market Tracker for Mug Bot running');

    // === Mug bot ingest server ===
    // The address is a SETTING, not a constant. Two reasons, both learned the hard way:
    //
    //   1. Moving the box used to mean editing tracked source, which leaves an uncommitted diff in
    //      the working tree on the VPS — exactly the state that makes update.sh refuse to deploy.
    //   2. Because it was a source edit rather than a setting, this script and item-matcher drifted
    //      onto DIFFERENT boxes without anything noticing. Gear captures went into the void for
    //      weeks: the script logs one console line on a network error and carries on.
    //
    // Stored in localStorage rather than GM storage so BOTH userscripts read the same value — they
    // share an origin (www.torn.com) but not GM storage. Not a secret: it is a public address, and
    // nothing sensitive is sent to it. The tracked constant below is only the fallback for a fresh
    // install; see the status pill in the page header for what is actually in use.
    const SERVER_URL_KEY = 'mugBotServerUrl';
    const SERVER_URL_DEFAULT = 'http://51.68.212.98:3001';

    function serverUrl() {
        const stored = (localStorage.getItem(SERVER_URL_KEY) || '').trim().replace(/\/+$/, '');
        return /^https?:\/\//.test(stored) ? stored : SERVER_URL_DEFAULT;
    }

    function promptForServerUrl() {
        const next = prompt('Mug bot ingest server address\n\n' +
                            '(shared with the item-matcher userscript)', serverUrl());
        if (next === null) return;
        const trimmed = next.trim().replace(/\/+$/, '');
        if (trimmed && !/^https?:\/\//.test(trimmed)) {
            alert('That does not look like a URL \u2014 it needs to start with http:// or https://');
            return;
        }
        if (trimmed) {
            localStorage.setItem(SERVER_URL_KEY, trimmed);
        } else {
            localStorage.removeItem(SERVER_URL_KEY);   // blank = back to the built-in default
        }
        serverReachable = true;       // unknown again until the next request says otherwise
        console.log('[MARKET TRACKER] server set to', serverUrl());
        updateToggleUI();
    }

    // Reachability, tracked so it can be SHOWN. A script that cannot reach its server has to say so
    // where you can see it; console.error alone is how the drift above went unnoticed.
    let serverReachable = true;

    /** Every POST to the mug bot goes through here, so there is exactly one place that knows
     *  whether the server is answering. */
    function postToServer(path, body, label) {
        GM.xmlHttpRequest({
            method: 'POST',
            url: `${serverUrl()}${path}`,
            data: JSON.stringify(body),
            headers: { 'Content-Type': 'application/json' },
            onload: (response) => {
                const ok = response.status >= 200 && response.status < 300;
                if (!ok) console.error(`[${label}] server returned HTTP ${response.status}`);
                if (ok !== serverReachable) { serverReachable = ok; updateToggleUI(); }
            },
            onerror: (response) => {
                console.error(`[${label}] cannot reach ${serverUrl()}`, response);
                if (serverReachable) { serverReachable = false; updateToggleUI(); }
            }
        });
    }

    // === COMPLIANCE: only ever act while THIS page is actively viewed ===
    // Torn's rules permit using/forwarding a page's WebSocket only while it is in the
    // foreground, visible and being interacted with. We gate every connect + forward on
    // this and disconnect the moment the page is hidden or loses focus. Do NOT remove.
    function isActivelyViewing() {
        return document.visibilityState === 'visible' && document.hasFocus();
    }
    // Status heartbeat cadence while actively viewing (server marks a page offline after ~90s).
    const HEARTBEAT_MS = 30000;

    let centrifuge = null;

    // Toggle functionality.
    //
    // localStorage, not GM storage. This previously read `GM.getValue ? GM.getValue(...) : ...`,
    // which only worked because GM.getValue is undefined without an @grant — had one ever been
    // added, GM.getValue returns a PROMISE, and a promise is always truthy, so tracking would have
    // silently become permanently ON. Same store as the server address, so both settings move
    // together.
    let trackingEnabled = localStorage.getItem('marketTrackerEnabled') !== 'false';

    function createToggleButton() {
        // Find a good place to add the toggle button
        let header = document.querySelector('.content-wrapper___QEUHr .header___nY6Z8') ||
                    document.querySelector('.content-wrapper___QEUHr') ||
                    document.querySelector('.item-market-wrap___QEUHr');

        if (!header) return;

        let toggleContainer = document.createElement('div');
        toggleContainer.id = 'mt-toggle-container';
        toggleContainer.style.cssText = `
            position: absolute;
            top: 10px;
            right: 10px;
            z-index: 1000;
            display: flex;
            align-items: center;
            gap: 8px;
        `;

        let toggleButton = document.createElement('button');
        toggleButton.id = 'mt-toggle-btn';
        toggleButton.classList.add('torn-btn');
        toggleButton.textContent = trackingEnabled ? '\uD83D\uDCE1 Tracking ON' : '\uD83D\uDEAB Tracking OFF';
        toggleButton.style.cssText = `
            background: ${trackingEnabled ? 'var(--default-green-btn)' : 'var(--default-red-btn)'};
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            font-weight: bold;
        `;

        let statusIndicator = document.createElement('div');
        statusIndicator.id = 'mt-status-indicator';
        statusIndicator.textContent = trackingEnabled ? 'Active' : 'Disabled';
        statusIndicator.style.cssText = `
            font-size: 11px;
            color: ${trackingEnabled ? '#00aa00' : '#aa0000'};
            font-weight: bold;
        `;

        toggleButton.addEventListener('click', function() {
            trackingEnabled = !trackingEnabled;
            updateToggleUI();

            localStorage.setItem('marketTrackerEnabled', trackingEnabled.toString());

            console.log(`[MARKET TRACKER] Tracking ${trackingEnabled ? 'enabled' : 'disabled'}`);

            // If disabling, disconnect any active connections
            if (!trackingEnabled) {
                disconnectWebSocket();
            } else {
                // If enabling, try to reconnect based on current page
                handleCategoryWebSocket();
            }
        });

        // Which server, and whether it is answering. The whole point of #15: a script posting into
        // the void must look different from a script that is working.
        let serverIndicator = document.createElement('div');
        serverIndicator.id = 'mt-server-indicator';
        serverIndicator.title = 'Click to change the mug bot server address';
        serverIndicator.style.cssText = 'font-size: 10px; cursor: pointer; font-weight: bold;';
        serverIndicator.addEventListener('click', promptForServerUrl);

        toggleContainer.appendChild(serverIndicator);
        toggleContainer.appendChild(statusIndicator);
        toggleContainer.appendChild(toggleButton);
        header.style.position = 'relative';
        header.appendChild(toggleContainer);
    }

    function updateToggleUI() {
        let toggleBtn = document.getElementById('mt-toggle-btn');
        let statusIndicator = document.getElementById('mt-status-indicator');

        if (toggleBtn) {
            toggleBtn.textContent = trackingEnabled ? '\uD83D\uDCE1 Tracking ON' : '\uD83D\uDEAB Tracking OFF';
            toggleBtn.style.background = trackingEnabled ? 'var(--default-green-btn)' : 'var(--default-red-btn)';
        }

        if (statusIndicator) {
            statusIndicator.textContent = trackingEnabled ? 'Active' : 'Disabled';
            statusIndicator.style.color = trackingEnabled ? '#00aa00' : '#aa0000';
        }

        let serverIndicator = document.getElementById('mt-server-indicator');
        if (serverIndicator) {
            const host = serverUrl().replace(/^https?:\/\//, '');
            if (!wsToken) {
                // No token means no subscription and therefore no data, ever. Without this the
                // script looks perfectly healthy while doing nothing at all.
                serverIndicator.textContent = '\u26a0 NO WS TOKEN';
                serverIndicator.style.color = '#ffaa00';
            } else {
                serverIndicator.textContent = serverReachable ? host : `\u26a0 ${host} UNREACHABLE`;
                serverIndicator.style.color = serverReachable ? '#888' : '#ff4444';
            }
        }
    }

    if (typeof GM !== 'undefined' && GM.registerMenuCommand) {
        GM.registerMenuCommand('Set mug-bot server URL', promptForServerUrl);
        GM.registerMenuCommand('Set Torn WebSocket token', promptForWsToken);
    }

    // Create toggle button when page loads
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', createToggleButton);
    } else {
        createToggleButton();
    }

    // Item ID to name mapping
    const itemMap = {
      106: 'Parachute',
      206: 'Xanax',
      283: 'Donator Pack',
      329: 'Skateboard',
      330: 'Boxing Gloves',
      331: 'Dumbbells',
      103: 'Firewalk Virus',
      366: 'Erotic DVD',
      367: 'Feathery Hotel Coupon',
      370: 'Drug Pack',
      396: 'Business Class Ticket',
      428: 'Casino Pass',
      532: 'Can of Red Cow',
      533: 'Can of Taurine Elite',
      554: 'Can of Rockstar Rudolph',
      555: 'Can of X-MASS',
      818: 'Six-Pack of Energy Drink',
      865: 'Poison Mistletoe',
      1029: 'Strippogram Voucher',
      1118: 'Armor Cache',
      1119: 'Melee Cache',
      1120: 'Small Arms Cache',
      1121: 'Medium Arms Cache',
      1122: 'Heavy Arms Cache'
    };

    // === WebSocket auth token (per Torn ACCOUNT, not per page) ===
    // JWT whose payload is {"sub":"<your Torn user id>"} — it identifies the logged-in user and is
    // reused for every channel subscription.
    //
    // In GM storage, NOT localStorage and NOT this file. Unlike the server address this IS a
    // secret: it authenticates as you. localStorage is readable by any script Torn itself loads,
    // and a per-install secret committed to shared source is the defect that had the original
    // author's token shipping to everyone who installed this.
    //
    // TODO(investigation): pull it live from the page instead of storing it at all, so it is always
    // the current session's own token (see README → "WebSocket token").
    let wsToken = '';
    GM.getValue('wsToken', '').then(v => {
        wsToken = (v || '').trim();
        updateToggleUI();
        if (wsToken) handleCategoryWebSocket();   // resolved after page load — connect now
        else console.warn('[MARKET TRACKER] no WebSocket token set \u2014 nothing will be tracked. ' +
                          'Userscript menu -> "Set Torn WebSocket token".');
    });

    function promptForWsToken() {
        const next = prompt('Torn Centrifugo WebSocket token (JWT)\n\n' +
                            'Per ACCOUNT, not per page. See the README for how to read yours off ' +
                            'the item market page. Stored in this userscript manager only.',
                            wsToken);
        if (next === null) return;
        wsToken = next.trim();
        GM.setValue('wsToken', wsToken);
        console.log('[MARKET TRACKER] WebSocket token', wsToken ? 'set' : 'cleared');
        updateToggleUI();
        if (wsToken) handleCategoryWebSocket(); else disconnectWebSocket();
    }

    // Observe for expanded item tiles to subscribe to the channel
    const itemObserver = new MutationObserver((mutations) => {
      // Skip if tracking is disabled
      if (!trackingEnabled) {
        return;
      }

      // COMPLIANCE: never connect/forward unless this page is actively viewed.
      if (!isActivelyViewing()) {
        disconnectWebSocket();
        return;
      }

      const expandedTile = document.querySelector('[class*="itemTile"][class*="expanded"]');
      if (expandedTile) {
        const nameDiv = expandedTile.querySelector('[class*="name"]');
        if (nameDiv) {
          const itemName = nameDiv.textContent.trim();
          const itemId = Object.keys(itemMap).find(id => itemMap[id] === itemName);
          if (itemId && wsToken) {
            // Check if we're already connected to this item
            if (window.hfCurrentConnection && window.hfCurrentConnection.type === 'item' && window.hfCurrentConnection.id === itemId) {
              return; // Already connected to this item
            }

            console.log('[MARKET TRACKER] Connecting to WS for', itemName, 'ID:', itemId);

            // Disconnect existing connection
            disconnectWebSocket();

            centrifuge = new Centrifuge('wss://ws-centrifugo.torn.com/connection/websocket', {
              token: wsToken,
              user: 'js',
              debug: true
            });
            centrifuge.connect();

            const channel = `item-market_${itemId}`;
            const sub = centrifuge.newSubscription(channel);
            sub.on('publication', (msg) => {
              // Filter out unwanted actions
              const message = msg.data.message || msg.data;
              const action = message.action;

              // Skip counter actions
              if (action === 'counterIncrease' || action === 'counterDecrease') {
                return;
              }

              // For update actions, only send if it's a quantity decrease (sale)
              // Must have listingID and available fields to be a valid quantity update
              if (action === 'update') {
                const updateData = message.data || [];
                const hasValidQuantityUpdate = updateData.some(update =>
                  update.listingID && update.available !== undefined && typeof update.available === 'number'
                );
                if (!hasValidQuantityUpdate) {
                  return; // Skip price-only updates or invalid updates
                }
              }

              console.log('[WS] Sending', action, 'for', channel);
              // Send to server
              postToServer('/market', { channel, data: [msg.data] }, 'WS');
            });
            sub.subscribe();
            console.log('[MARKET TRACKER] Subscribed to', channel);

            // Send initial ping to status
            postToServer('/status', { tracked: [parseInt(itemId)] }, 'STATUS');

            // Heartbeat ONLY while actively viewing (compliance). Stops + disconnects the
            // moment the page is no longer actively viewed, so status-tracker goes red.
            const pingInterval = setInterval(() => {
              if (!isActivelyViewing()) { disconnectWebSocket(); return; }
              postToServer('/status', { tracked: [parseInt(itemId)] }, 'STATUS');
            }, HEARTBEAT_MS);

            // Store interval for cleanup
            if (!window.hfPingIntervals) window.hfPingIntervals = {};
            window.hfPingIntervals[itemId] = pingInterval;

            // Store current connection info
            window.hfCurrentConnection = { type: 'item', id: itemId, name: itemName };
          } else {
            console.log('[MARKET TRACKER] Item not in map or no WS token:', itemName, itemId, wsToken);
          }
        }
      } else {
        // No expanded tile, disconnect if connected to an item
        if (window.hfCurrentConnection && window.hfCurrentConnection.type === 'item') {
          console.log('[MARKET TRACKER] No expanded item tile, disconnecting from item WebSocket');
          disconnectWebSocket();
        }
      }
    });

    // Handle category WebSocket connections
    function connectCategoryWebSocket(categoryName) {
        if (!wsToken || !trackingEnabled || !isActivelyViewing()) return;

        console.log('[MARKET TRACKER] Connecting to WS for category:', categoryName);

        // Disconnect existing connection if switching categories
        disconnectWebSocket();

        centrifuge = new Centrifuge('wss://ws-centrifugo.torn.com/connection/websocket', {
            token: wsToken,
            user: 'js',
            debug: true
        });
        centrifuge.connect();

        const channel = `item-market_${categoryName}`;
        const sub = centrifuge.newSubscription(channel);
        sub.on('publication', (msg) => {
            const message = msg.data.message || msg.data;
            const action = message.action;

            // For update actions, only send if it's a quantity decrease (sale)
            // Must have listingID and available fields to be a valid quantity update
            if (action === 'update') {
                const updateData = message.data || [];
                const hasValidQuantityUpdate = updateData.some(update =>
                    update.listingID && update.available !== undefined && typeof update.available === 'number'
                );
                if (!hasValidQuantityUpdate) {
                    return; // Skip price-only updates or invalid updates
                }
            }

            console.log('[WS] Sending', action, 'for category', channel);
              // Send to server
              postToServer('/market', { channel, data: [msg.data] }, 'WS');
        });
        sub.subscribe();
        console.log('[MARKET TRACKER] Subscribed to category channel:', channel);

        // Send initial ping to status
        postToServer('/status', { tracked: [categoryName] }, 'STATUS');

        // Heartbeat ONLY while actively viewing (compliance). Stops + disconnects the
        // moment the page is no longer actively viewed, so status-tracker goes red.
        const pingInterval = setInterval(() => {
            if (!isActivelyViewing()) { disconnectWebSocket(); return; }
            postToServer('/status', { tracked: [categoryName] }, 'STATUS');
        }, HEARTBEAT_MS);

        // Store interval for cleanup
        if (!window.hfPingIntervals) window.hfPingIntervals = {};
        window.hfPingIntervals[categoryName] = pingInterval;

        // Store current connection info
        window.hfCurrentConnection = { type: 'category', name: categoryName };
    }

    // Disconnect WebSocket and clean up
    function disconnectWebSocket() {
        if (centrifuge) {
            console.log('[MARKET TRACKER] Disconnecting existing WebSocket connection');
            centrifuge.disconnect();
            centrifuge = null;
        }

        // Clear all ping intervals
        if (window.hfPingIntervals) {
            Object.values(window.hfPingIntervals).forEach(interval => clearInterval(interval));
            window.hfPingIntervals = {};
        }

        // Clear current connection info
        window.hfCurrentConnection = null;
    }

    // Connect to category WebSockets based on URL
    function handleCategoryWebSocket() {
        // Skip if tracking is disabled or this page isn't actively viewed (compliance).
        if (!trackingEnabled || !isActivelyViewing()) {
            return;
        }

        const url = window.location.href;

        // Only connect WebSocket on specific category URLs with exact parameters
        if (url.includes('view=category&categoryName=Primary&sortField=price&sortOrder=ASC&bonuses[0]=-2')) {
            connectCategoryWebSocket('Primary');
        } else if (url.includes('view=category&categoryName=Secondary&sortField=price&sortOrder=ASC&bonuses[0]=-2')) {
            connectCategoryWebSocket('Secondary');
        } else if (url.includes('view=category&categoryName=Melee&sortField=price&sortOrder=ASC&bonuses[0]=-2')) {
            connectCategoryWebSocket('Melee');
        } else if (url.includes('view=category&categoryName=Defensive&sortField=price&sortOrder=ASC')) {
            connectCategoryWebSocket('Defensive');
        }
    }

    // Handle category WebSocket on page load
    handleCategoryWebSocket();

    // Attach to the item list container
    const itemList = document.querySelector('.itemList___u4Hg1');
    if (itemList) {
      itemObserver.observe(itemList, { childList: true, subtree: true });
    } else {
      // Fallback to body
      itemObserver.observe(document.body, { childList: true, subtree: true });
    }

    // Watch for URL changes (SPA navigation)
    let currentUrl = window.location.href;
    const urlObserver = new MutationObserver(() => {
        if (window.location.href !== currentUrl) {
            const oldUrl = currentUrl;
            currentUrl = window.location.href;
            console.log('[MARKET TRACKER] URL changed from:', oldUrl, 'to:', currentUrl);

            // Check if we need to disconnect WebSocket
            const wasValidCategoryPage =
                oldUrl.includes('view=category&categoryName=Melee&sortField=price&sortOrder=ASC&bonuses[0]=-2') ||
                oldUrl.includes('view=category&categoryName=Primary&sortField=price&sortOrder=ASC&bonuses[0]=-2') ||
                oldUrl.includes('view=category&categoryName=Secondary&sortField=price&sortOrder=ASC&bonuses[0]=-2') ||
                oldUrl.includes('view=category&categoryName=Defensive&sortField=price&sortOrder=ASC');

            const isValidCategoryPage =
                currentUrl.includes('view=category&categoryName=Melee&sortField=price&sortOrder=ASC&bonuses[0]=-2') ||
                currentUrl.includes('view=category&categoryName=Primary&sortField=price&sortOrder=ASC&bonuses[0]=-2') ||
                currentUrl.includes('view=category&categoryName=Secondary&sortField=price&sortOrder=ASC&bonuses[0]=-2') ||
                currentUrl.includes('view=category&categoryName=Defensive&sortField=price&sortOrder=ASC');

            // Disconnect if leaving a valid category page or switching between different valid pages
            if (wasValidCategoryPage && (!isValidCategoryPage || window.hfCurrentConnection)) {
                console.log('[MARKET TRACKER] Disconnecting WebSocket due to URL change');
                disconnectWebSocket();
            }

            handleCategoryWebSocket();
        }
    });
    urlObserver.observe(document.body, { childList: true, subtree: true });

    // === COMPLIANCE: disconnect the instant this page stops being actively viewed ===
    // (tab hidden / window blurred / minimized) and reconnect when it returns to the
    // foreground. This guarantees the WebSocket only ever runs for the page you're looking at.
    document.addEventListener('visibilitychange', () => {
        if (!isActivelyViewing()) {
            disconnectWebSocket();
        } else {
            handleCategoryWebSocket();
        }
    });
    window.addEventListener('blur', () => {
        disconnectWebSocket();
    });
    window.addEventListener('focus', () => {
        if (isActivelyViewing()) handleCategoryWebSocket();
    });

})();
