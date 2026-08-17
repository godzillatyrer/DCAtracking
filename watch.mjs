#!/usr/bin/env node
/**
 * ansem-tier-watch
 *
 * Alerts to Telegram when a coin appears in the Gold or Diamond tier on ansem.io.
 *
 * Data source: https://ansem.io/api/coins  (undocumented public JSON endpoint)
 *   - returns { coins: [ { slug, name, ticker, mint, tier, status, marketCapUsd,
 *                          volume24hUsd, change24hPct, txns24h, teamPct, airdropPct,
 *                          airdropTotal, curvePct, creatorWallet, imageUrl,
 *                          enhancedAt, createdAt } ] }
 *   - tier is exactly "free" | "gold" | "diamond"
 *   - `limit` query param is honoured, max 200. There is NO server-side tier filter
 *     (passing ?tier=gold is silently ignored), so we filter client-side.
 *   - the endpoint is served by multiple replicas that can disagree slightly, so the
 *     response count fluctuates between calls. We therefore key alerts off a
 *     persistent seen-set rather than diffing consecutive responses.
 *
 * Alert key is `mint:tier`, which means we fire both when a brand-new coin lands in
 * Gold/Diamond AND when an existing coin is upgraded into Gold/Diamond later.
 *
 * Requires Node 18+ (global fetch). No dependencies.
 */

import fs from 'node:fs/promises';
import path from 'node:path';

// ---------------------------------------------------------------- config

const CFG = {
  botToken: process.env.TELEGRAM_BOT_TOKEN || '',
  chatId: process.env.TELEGRAM_CHAT_ID || '',
  telegramBase: process.env.TELEGRAM_API_BASE || 'https://api.telegram.org',
  fetchAttempts: clampInt(process.env.FETCH_ATTEMPTS, 4, 1, 8),
  apiUrl: process.env.ANSEM_API_URL || 'https://ansem.io/api/coins',
  siteUrl: process.env.ANSEM_SITE_URL || 'https://ansem.io',
  limit: clampInt(process.env.ANSEM_LIMIT, 200, 1, 200),
  tiers: (process.env.WATCH_TIERS || 'gold,diamond')
    .split(',').map(s => s.trim().toLowerCase()).filter(Boolean),
  pollMs: clampInt(process.env.POLL_INTERVAL_MS, 30_000, 5_000, 3_600_000),
  stateFile: process.env.STATE_FILE || path.join(process.cwd(), 'state.json'),
  dryRun: /^(1|true|yes)$/i.test(process.env.DRY_RUN || ''),
  // On a cold start the API already contains history. Alerting on all of it would
  // spam you, so by default the first run only records what it sees.
  alertOnFirstRun: /^(1|true|yes)$/i.test(process.env.ALERT_ON_FIRST_RUN || ''),
  once: /^(1|true|yes)$/i.test(process.env.RUN_ONCE || ''),
  verbose: /^(1|true|yes)$/i.test(process.env.VERBOSE || ''),
};

function clampInt(raw, dflt, min, max) {
  const n = Number.parseInt(raw ?? '', 10);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(max, Math.max(min, n));
}

// ---------------------------------------------------------------- logging

const log = (...a) => console.log(new Date().toISOString(), ...a);
const vlog = (...a) => { if (CFG.verbose) log('[debug]', ...a); };

// ---------------------------------------------------------------- state

/** @typedef {{ seen: Record<string, {tier:string, ticker:string, firstAlertedAt:string}>, lastRunAt: string|null, runs: number }} State */

const EMPTY_STATE = { seen: {}, lastRunAt: null, runs: 0 };

async function loadState() {
  try {
    const raw = await fs.readFile(CFG.stateFile, 'utf8');
    const parsed = JSON.parse(raw);
    return { ...EMPTY_STATE, ...parsed, seen: parsed.seen ?? {} };
  } catch (err) {
    if (err.code !== 'ENOENT') {
      // A corrupt state file is worse than no state file, but silently wiping it
      // would re-spam. Fail loudly and let the operator decide.
      throw new Error(`Could not read state file ${CFG.stateFile}: ${err.message}`);
    }
    return { ...EMPTY_STATE };
  }
}

async function saveState(state) {
  const tmp = `${CFG.stateFile}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(state, null, 2));
  await fs.rename(tmp, CFG.stateFile); // atomic-ish: survives a kill mid-write
}

// ---------------------------------------------------------------- fetching

async function fetchWithRetry(url, opts = {}, attempts = CFG.fetchAttempts) {
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    if (i > 0) {
      const backoff = Math.min(30_000, 1_000 * 2 ** (i - 1));
      vlog(`retry ${i} in ${backoff}ms`);
      await sleep(backoff);
    }
    try {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 20_000);
      const res = await fetch(url, { ...opts, signal: ctl.signal });
      clearTimeout(timer);
      if (res.status >= 500 || res.status === 429) {
        lastErr = new Error(`HTTP ${res.status}`);
        continue;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
      return res;
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr ?? new Error('fetch failed');
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function fetchCoins() {
  const url = `${CFG.apiUrl}?limit=${CFG.limit}`;
  const res = await fetchWithRetry(url, {
    headers: { accept: 'application/json', 'user-agent': 'ansem-tier-watch/1.0' },
  });
  const body = await res.json();
  if (!body || !Array.isArray(body.coins)) {
    throw new Error(`Unexpected response shape: ${JSON.stringify(body).slice(0, 200)}`);
  }
  return body.coins;
}

// ---------------------------------------------------------------- formatting

const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function money(n) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  const v = Number(n);
  if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  return `$${v.toFixed(2)}`;
}

function pct(n) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  const v = Number(n);
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

function tierBadge(tier) {
  return { gold: '🥇 GOLD', diamond: '💎 DIAMOND', free: '⚪ FREE' }[tier] ?? tier.toUpperCase();
}

function buildMessage(coin, { upgraded }) {
  const url = `${CFG.siteUrl}/launch/coin/${coin.mint}`;
  const headline = upgraded
    ? `${tierBadge(coin.tier)} — tier upgrade`
    : `${tierBadge(coin.tier)} — new listing`;

  const lines = [
    `<b>${esc(headline)}</b>`,
    '',
    `<b>${esc(coin.name || coin.ticker || coin.slug)}</b> · $${esc(coin.ticker ?? '?')}`,
    `Market cap: <b>${money(coin.marketCapUsd)}</b>   24h: <b>${pct(coin.change24hPct)}</b>`,
    `Volume 24h: ${money(coin.volume24hUsd)}   Txns: ${coin.txns24h ?? '—'}`,
    `Status: ${esc(coin.status ?? '—')}   Curve: ${coin.curvePct != null ? `${Number(coin.curvePct).toFixed(1)}%` : '—'}`,
    `Team: ${coin.teamPct != null ? `${coin.teamPct}%` : '—'}   Airdrop: ${coin.airdropPct != null ? `${coin.airdropPct}%` : '—'}`,
    '',
    `<code>${esc(coin.mint)}</code>`,
    `<a href="${esc(url)}">Open on ansem.io</a>`,
  ];
  return lines.join('\n');
}

// ---------------------------------------------------------------- telegram

async function sendTelegram(text) {
  if (CFG.dryRun) {
    log('[dry-run] would send:\n' + text.replace(/<[^>]+>/g, ''));
    return;
  }
  const res = await fetchWithRetry(
    `${CFG.telegramBase}/bot${CFG.botToken}/sendMessage`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        chat_id: CFG.chatId,
        text,
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      }),
    },
  );
  const body = await res.json();
  if (!body.ok) throw new Error(`Telegram rejected the message: ${JSON.stringify(body).slice(0, 300)}`);
}

// ---------------------------------------------------------------- core loop

async function checkOnce(state) {
  const coins = await fetchCoins();
  const wanted = coins.filter(c => c.tier && CFG.tiers.includes(String(c.tier).toLowerCase()));
  vlog(`fetched ${coins.length} coins, ${wanted.length} in [${CFG.tiers.join(', ')}]`);

  const isFirstRun = state.runs === 0;
  const fresh = [];

  for (const coin of wanted) {
    if (!coin.mint) continue;
    const key = `${coin.mint}:${String(coin.tier).toLowerCase()}`;
    if (state.seen[key]) continue;

    // Was this mint already known to us at a different (lower) tier?
    const upgraded = Object.keys(state.seen).some(k => k.startsWith(`${coin.mint}:`));

    state.seen[key] = {
      tier: String(coin.tier).toLowerCase(),
      ticker: coin.ticker ?? null,
      firstAlertedAt: new Date().toISOString(),
    };
    fresh.push({ coin, upgraded });
  }

  state.runs += 1;
  state.lastRunAt = new Date().toISOString();

  // Persist BEFORE sending. If Telegram is down we would rather miss one alert
  // than replay every alert on the next tick.
  await saveState(state);

  if (isFirstRun && !CFG.alertOnFirstRun) {
    log(`first run: recorded ${fresh.length} existing ${CFG.tiers.join('/')} coin(s) without alerting ` +
        `(set ALERT_ON_FIRST_RUN=true to change this)`);
    return 0;
  }

  for (const item of fresh) {
    try {
      await sendTelegram(buildMessage(item.coin, item));
      log(`alerted: ${item.coin.ticker} (${item.coin.tier})${item.upgraded ? ' [upgrade]' : ''}`);
    } catch (err) {
      log(`ERROR sending alert for ${item.coin.ticker}: ${err.message}`);
      // Roll back just this key so the next tick retries it.
      delete state.seen[`${item.coin.mint}:${String(item.coin.tier).toLowerCase()}`];
      await saveState(state);
    }
    await sleep(1_200); // stay under Telegram's ~30 msg/sec and per-chat limits
  }
  return fresh.length;
}

function validateConfig() {
  const problems = [];
  if (!CFG.dryRun && !CFG.botToken) problems.push('TELEGRAM_BOT_TOKEN is not set');
  if (!CFG.dryRun && !CFG.chatId) problems.push('TELEGRAM_CHAT_ID is not set');
  if (CFG.tiers.length === 0) problems.push('WATCH_TIERS resolved to an empty list');
  const known = ['free', 'gold', 'diamond'];
  const unknown = CFG.tiers.filter(t => !known.includes(t));
  if (unknown.length) problems.push(`WATCH_TIERS contains unknown tier(s): ${unknown.join(', ')} (known: ${known.join(', ')})`);
  if (problems.length) {
    console.error('Configuration problems:\n  - ' + problems.join('\n  - '));
    process.exit(1);
  }
}

async function main() {
  validateConfig();
  log(`ansem-tier-watch starting`);
  log(`  tiers      : ${CFG.tiers.join(', ')}`);
  log(`  poll       : every ${Math.round(CFG.pollMs / 1000)}s`);
  log(`  state      : ${CFG.stateFile}`);
  log(`  mode       : ${CFG.dryRun ? 'DRY RUN (no Telegram)' : 'live'}${CFG.once ? ' / single pass' : ''}`);

  const state = await loadState();

  let stopping = false;
  for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => {
      if (stopping) process.exit(1);
      stopping = true;
      log(`${sig} received, finishing current cycle then exiting`);
    });
  }

  for (;;) {
    try {
      await checkOnce(state);
    } catch (err) {
      log(`ERROR during check: ${err.message}`);
    }
    if (CFG.once || stopping) break;
    await sleep(CFG.pollMs);
    if (stopping) break;
  }
  log('exiting');
}

main().catch(err => {
  console.error('fatal:', err);
  process.exit(1);
});
