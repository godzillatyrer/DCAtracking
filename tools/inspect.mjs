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

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Cloudflare challenges some fraction of requests rather than all of them, so a
// single probe tells you almost nothing. Probe a handful of times and report the
// hit rate — that ratio is the actual health metric for feed access, and it is
// what decides whether FETCH_ATTEMPTS=4 in watch.mjs is generous or marginal.
const ATTEMPTS = Number(process.env.PROBE_ATTEMPTS || 6);

console.log(`GET ${URL_}`);
console.log(`probing ${ATTEMPTS}x to measure the Cloudflare challenge rate\n`);

let body = null;
let challenges = 0;

for (let i = 0; i < ATTEMPTS; i++) {
  if (i) await sleep(1_000);
  let res, raw;
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 20_000);
    res = await fetch(URL_, { headers: HEADERS, signal: ctl.signal });
    clearTimeout(timer);
    raw = await res.text();
  } catch (err) {
    challenges++;
    console.log(`  ${i + 1}. network error: ${err.message}`);
    continue;
  }

  const ctype = res.headers.get('content-type') || '(none)';
  const mitigated = res.headers.get('cf-mitigated');

  if (ctype.includes('json')) {
    console.log(`  ${i + 1}. ${res.status} ok  (${ctype})`);
    body ??= JSON.parse(raw);
    continue;
  }

  challenges++;
  const isChallenge = /just a moment|challenges\.cloudflare\.com|cf_chl_opt/i.test(raw);
  console.log(`  ${i + 1}. ${res.status} ${isChallenge ? 'CHALLENGED' : 'non-JSON'}` +
              `  (${ctype}${mitigated ? `, cf-mitigated: ${mitigated}` : ''})`);
  if (!isChallenge) console.log(`     first 120 bytes: ${raw.slice(0, 120)}`);
}

const rate = (challenges / ATTEMPTS * 100).toFixed(0);
console.log(`\nchallenge rate: ${challenges}/${ATTEMPTS} (${rate}%)`);
if (challenges === ATTEMPTS) {
  console.log('Every probe was blocked. watch.mjs will be failing too — check the\n' +
              'Render logs for "ERROR during check". Alerts are NOT flowing.');
  process.exit(2);
}
if (challenges > 0) {
  // p(all 4 attempts challenged) with an independence assumption — rough, but
  // the right order of magnitude for deciding whether to raise FETCH_ATTEMPTS.
  const pMiss = (challenges / ATTEMPTS) ** 4;
  console.log(`At this rate a full 4-attempt cycle fails ~${(pMiss * 100).toFixed(1)}% of ticks.`);
}

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
