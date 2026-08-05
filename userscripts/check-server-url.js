/**
 * Both userscripts resolve the mug-bot address the same way — checked, not assumed.
 *
 * #15 was not really about a hardcoded constant. It was about the two scripts silently drifting
 * onto DIFFERENT boxes, with nothing detecting it until someone read the source. They now share a
 * localStorage key, but they still each carry their own copy of the resolver, so the drift is
 * still possible — just one edit away instead of two.
 *
 * This lifts `serverUrl()` out of each file and runs the same table through both. It is deliberately
 * a source-level check: nothing here imports the scripts (they touch `document` at load), and a
 * fake `localStorage` is all the resolver actually needs.
 *
 *   node userscripts/check-server-url.js
 */

const fs = require('fs');
const path = require('path');

const DEFAULT = 'http://51.68.212.98:3001';
const FILES = ['market-tracker.user.js', 'item-matcher.user.js'];

// [stored value, expected result, what it protects against]
const CASES = [
    [null, DEFAULT, 'unset -> the tracked default'],
    ['', DEFAULT, 'blank -> the tracked default'],
    ['   ', DEFAULT, 'whitespace only -> the tracked default'],
    ['not-a-url', DEFAULT, 'garbage -> the default, not a broken URL'],
    // A scheme check, not decoration: the value is interpolated straight into a request URL, and
    // localStorage is writable by any script the page loads.
    ['javascript:alert(1)', DEFAULT, 'a non-http scheme is refused'],
    ['ftp://box.example', DEFAULT, 'a non-http scheme is refused'],
    ['http://1.2.3.4:3001', 'http://1.2.3.4:3001', 'a plain override is honoured'],
    ['https://box.example', 'https://box.example', 'https is honoured'],
    // Trailing slashes matter: every caller appends "/gear", "/status", ... so one slash too many
    // produces "//gear" and a 404 that looks exactly like the server being broken.
    ['http://1.2.3.4:3001/', 'http://1.2.3.4:3001', 'a trailing slash is stripped'],
    ['  https://box.example///  ', 'https://box.example', 'whitespace and repeated slashes'],
];

function resolverFrom(file) {
    const src = fs.readFileSync(path.join(__dirname, file), 'utf8');
    const body = src.match(/const SERVER_URL_KEY[\s\S]*?\n {4}}\n/);
    if (!body) {
        throw new Error(`${file}: could not find the serverUrl() block — did it get renamed?`);
    }
    return (stored) => new Function('localStorage', body[0] + '; return serverUrl();')(
        { getItem: () => stored });
}

let failures = 0;
const results = {};

for (const file of FILES) {
    const serverUrl = resolverFrom(file);
    results[file] = CASES.map(([stored, want, label]) => {
        const got = serverUrl(stored);
        if (got !== want) {
            failures++;
            console.error(`FAIL ${file}: ${label}\n     stored=${JSON.stringify(stored)} ` +
                          `want=${want} got=${got}`);
        }
        return got;
    });
}

// The drift check itself: same input, same answer, in both scripts.
const [a, b] = FILES;
results[a].forEach((got, i) => {
    if (got !== results[b][i]) {
        failures++;
        console.error(`FAIL the two scripts disagree on ${JSON.stringify(CASES[i][0])}: ` +
                      `${a} -> ${got}, ${b} -> ${results[b][i]}`);
    }
});

if (failures) {
    console.error(`\n${failures} failure(s)`);
    process.exit(1);
}
console.log(`ok — ${FILES.length} scripts x ${CASES.length} cases, and they agree`);
