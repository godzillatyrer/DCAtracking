#!/usr/bin/env node
// Prints the shape of the live /api/coins feed. Read-only, no state, no Telegram.
//
// Answers three questions the watcher's design depends on:
//   1. What distinguishes a paid "Get Listed" coin from a launchpad launch?
//   2. Is the feed sorted newest-first? (If not, the 200-item cap can hide
//      new listings entirely, because there is no working offset/page param.)
//   3. Are we being served real JSON, or a Cloudflare bot challenge?
//
// Run it where egress is unrestricted:  npm run inspect

const URL_ = `${process.env.ANSEM_API_URL || 'https://ansem.io/api/coins'}?limit=${process.env.ANSEM_LIMIT || 200}`;

// These headers are not decoration. A bare fetch with node's default
// user-agent gets a Cloudflare "Just a moment..." interstitial instead of
// JSON; sending the same headers watch.mjs sends gets through.
const HEADERS = { accept: 'application/json', 'user-agent': 'ansem-tier-watch/1.0' };

const ctl = new AbortController();
const timer = setTimeout(() => ctl.abort(), 20_000);
const res = await fetch(URL_, { headers: HEADERS, signal: ctl.signal });
clearTimeout(timer);

const ctype = res.headers.get('content-type') || '(none)';
console.log(`GET ${URL_}`);
console.log(`  status       : ${res.status} ${res.statusText}`);
console.log(`  content-type : ${ctype}`);
const mitigated = res.headers.get('cf-mitigated');
if (mitigated) console.log(`  cf-mitigated : ${mitigated}`);

const raw = await res.text();

if (!ctype.includes('json')) {
  const challenged = /just a moment|challenges\.cloudflare\.com|cf_chl_opt/i.test(raw);
  console.log('');
  console.log(challenged
    ? 'BLOCKED: Cloudflare served a bot challenge, not the API.\n' +
      'The request was rejected before reaching the app. Retrying immediately\n' +
      'will usually be rejected too — this is the failure mode to watch for.'
    : `Not JSON. First 300 bytes:\n${raw.slice(0, 300)}`);
  process.exit(2);
}

const body = JSON.parse(raw);
const coins = body?.coins;
if (!Array.isArray(coins)) {
  console.log(`\nUnexpected shape — no "coins" array. Keys: ${Object.keys(body || {}).join(', ')}`);
  process.exit(2);
}

console.log(`\ntotal coins  : ${coins.length}`);
if (coins.length >= Number(process.env.ANSEM_LIMIT || 200)) {
  console.log('  ^ saturated at the cap: the feed is larger than what we can see,');
  console.log('    and there is no working offset/page param.');
}

// --- what shapes exist, and how many of each -------------------------------
const buckets = new Map();
for (const c of coins) {
  const key = [
    `tier=${c.tier}`,
    `status=${c.status ?? 'null'}`,
    c.enhancedAt ? 'enhanced' : 'not-enhanced',
    c.pairAddress ? 'has-pair' : 'no-pair',
    c.curvePct == null ? 'no-curve' : 'on-curve',
  ].join('  ');
  buckets.set(key, (buckets.get(key) || 0) + 1);
}
console.log('\ncount  shape');
console.log('-----  -----------------------------------------------------------');
for (const [key, n] of [...buckets].sort((a, b) => b[1] - a[1])) {
  console.log(String(n).padStart(5), key);
}

// --- sort order: does the cap hide new listings? ---------------------------
const stamps = coins.map(c => c.createdAt).filter(Boolean);
if (stamps.length >= 2) {
  const first = Date.parse(stamps[0]);
  const last = Date.parse(stamps[stamps.length - 1]);
  console.log('\ncreatedAt of first 3 :', stamps.slice(0, 3).join('  '));
  console.log('createdAt of last 3  :', stamps.slice(-3).join('  '));
  console.log(Number.isFinite(first) && Number.isFinite(last)
    ? (first > last
        ? '  => newest-first. New listings enter at the top, so the cap is safe.'
        : '  => NOT newest-first. The 200-cap can hide a new listing entirely.')
    : '  => could not parse timestamps');
}

// --- the paid-listing candidates, in full ----------------------------------
const candidates = coins.filter(c => c.enhancedAt || (c.pairAddress && c.curvePct == null));
console.log(`\npaid-listing candidates (enhancedAt set, or off-curve with a pair): ${candidates.length}`);
for (const c of candidates.slice(0, 5)) {
  console.log('  ' + JSON.stringify({
    ticker: c.ticker, tier: c.tier, status: c.status, enhancedAt: c.enhancedAt,
    curvePct: c.curvePct, pairAddress: c.pairAddress, createdAt: c.createdAt,
  }));
}

const tiers = new Set(coins.map(c => String(c.tier).toLowerCase()));
const unknown = [...tiers].filter(t => !['free', 'gold', 'diamond'].includes(t));
if (unknown.length) console.log(`\nWARNING: unknown tier value(s) present: ${unknown.join(', ')}`);
