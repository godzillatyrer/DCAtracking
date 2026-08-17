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

  // Alert when a coin appears that did NOT come off the bonding curve — i.e. an
  // existing Solana token that paid the "Get Listed" fee. Independent of tier,
  // because the observed feed shows paid features and tier are not the same axis.
  watchListings: !/^(0|false|no)$/i.test(process.env.WATCH_LISTINGS || 'true'),

  // The whole thing rides on an undocumented endpoint. If a `tier` or `status`
  // value we have never seen shows up, say so rather than silently not matching.
  schemaDriftAlert: !/^(0|false|no)$/i.test(process.env.SCHEMA_DRIFT_ALERT || 'true'),

  // Telegram us if no fetch has succeeded in this long. The feed is a rolling
  // window of roughly 50 minutes (200 coins at ~4/min), so an outage shorter than
  // that loses nothing — the coin is still there when we come back. 10 minutes
  // leaves a wide margin while still being noticed the same hour.
  staleAlertMs: clampInt(process.env.STALE_ALERT_MS, 600_000, 60_000, 86_400_000),

  // Periodic "still alive" message so silence is distinguishable from breakage.
  // 0 disables it.
  heartbeatMs: clampInt(process.env.HEARTBEAT_MS, 86_400_000, 0, 604_800_000),

  // $ANSEM burns, read from /api/leaderboard/burners. Burning is how a team pays
  // the listing fee and how it climbs the tier ladder, so a burn is the earliest
  // warning that a listing is coming.
  watchBurns: !/^(0|false|no)$/i.test(process.env.WATCH_BURNS || 'true'),
  // Ignore dust. A burn that crosses a tier threshold or the listing fee is
  // always reported regardless of this floor.
  burnMinUsd: clampInt(process.env.BURN_MIN_USD, 1_000, 0, 1_000_000),

  // /api/listing/config.enabled flipping true is the moment listings reopen;
  // /api/gate carries a site-wide countdown. Neither changes often, so they are
  // polled every Nth tick rather than every tick — the endpoint is behind
  // Cloudflare and request volume is not free.
  watchStatus: !/^(0|false|no)$/i.test(process.env.WATCH_STATUS || 'true'),
  statusEvery: clampInt(process.env.STATUS_POLL_EVERY, 10, 1, 1_000),
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

// A factory, not a constant: `knownStatuses` is an array, and spreading a shared
// constant would hand every caller the same one to mutate.
const emptyState = () => ({
  seen: {},
  lastRunAt: null,
  runs: 0,
  // Persisted rather than in-memory: a crash-loop restarts the process, and an
  // in-memory clock would reset each time and never trip the staleness alarm.
  lastOkAt: null,
  staleAlertedAt: null,
  lastHeartbeatAt: null,
  // Every `status` value observed so far. Anything outside this set is either a
  // new product surface (a paid listing looks different from a curve launch) or
  // a schema change — both worth a message.
  knownStatuses: [],

  // wallet -> cumulative $ANSEM burned, as last seen. Diffed each tick.
  burners: {},
  // Recent burns, newest last, so a listing can name the payment that bought it.
  // Anyone may burn for any mint, so this is the only honest link between the two
  // events: proximity in time, stated as such.
  recentBurns: [],
  // Live values from /api/config, refreshed periodically. Never hardcode these:
  // the site renders 25,000/100,000 on /burn while the API returns 92,627/370,508,
  // because the API recomputes them from the current $ANSEM price.
  tierThresholds: null,
  ansemPriceUsd: null,
  listingEnabled: null,
  gate: null,
});

async function loadState() {
  try {
    const raw = await fs.readFile(CFG.stateFile, 'utf8');
    const parsed = JSON.parse(raw);
    return {
      ...emptyState(),
      ...parsed,
      seen: parsed.seen ?? {},
      knownStatuses: Array.isArray(parsed.knownStatuses) ? parsed.knownStatuses : [],
      burners: parsed.burners ?? {},
      recentBurns: Array.isArray(parsed.recentBurns) ? parsed.recentBurns : [],
    };
  } catch (err) {
    if (err.code !== 'ENOENT') {
      // A corrupt state file is worse than no state file, but silently wiping it
      // would re-spam. Fail loudly and let the operator decide.
      throw new Error(`Could not read state file ${CFG.stateFile}: ${err.message}`);
    }
    return emptyState();
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
      if (!res.ok) {
        // 404 is a permanent answer — the endpoint is not there, and asking three
        // more times just multiplies load against a Cloudflare-fronted origin.
        // Deliberately narrow: 403 is what a Cloudflare *challenge* returns, and
        // those are transient and must keep retrying.
        const err = new Error(`HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
        if (res.status === 404) err.permanent = true;
        throw err;
      }
      return res;
    } catch (err) {
      lastErr = err;
      if (err.permanent) break;
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

// ------------------------------------------------------------- side channels
//
// Everything below reads an endpoint other than /api/coins. Each one is wrapped
// by its caller so a shape change or a 404 can never take down the tier watcher,
// which is the part that has actually been verified against production.

const apiBase = () => CFG.apiUrl.replace(/\/coins\/?$/, '');

async function fetchJson(pathSuffix) {
  const res = await fetchWithRetry(`${apiBase()}${pathSuffix}`, {
    headers: { accept: 'application/json', 'user-agent': 'ansem-tier-watch/1.0' },
  });
  return res.json();
}

function num(n, digits = 0) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  return Number(n).toLocaleString('en-US', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

/**
 * $ANSEM burns.
 *
 * The site's own copy says burns "go to the community burn wallet", but on-chain
 * they are plain `BurnChecked` instructions with no destination — total supply
 * simply drops. A monitor watching a recipient wallet would therefore sit silent
 * forever and never error. We sidestep the question entirely by reading the
 * site's own burners leaderboard, which is authoritative for what it counts and
 * gives us the wallet, which a supply diff would not.
 *
 * Amounts are cumulative per wallet, so a burn is an *increase*, not a new row.
 */
async function checkBurns(state, isFirstRun) {
  const body = await fetchJson('/leaderboard/burners');
  const rows = Array.isArray(body?.burners) ? body.burners : null;
  if (!rows) {
    vlog('burners: no "burners" array in response, skipping');
    return [];
  }

  // Number(null) is 0, not NaN. Left unguarded that makes every burn worth $0
  // and silently dust-filtered the moment /api/config is unreachable — losing
  // precisely the alert this exists for. An unknown price must mean "unknown".
  const rawPrice = Number(state.ansemPriceUsd);
  const price = Number.isFinite(rawPrice) && rawPrice > 0 ? rawPrice : null;
  const gold = Number(state.tierThresholds?.bronze);      // "bronze" is Gold internally
  const diamond = Number(state.tierThresholds?.diamond);
  const notices = [];

  for (const row of rows) {
    const wallet = row?.wallet;
    const total = Number(row?.amount);
    if (!wallet || !Number.isFinite(total)) continue;

    const prev = Number(state.burners[wallet] ?? 0);
    state.burners[wallet] = total;
    if (isFirstRun) continue;

    const delta = total - prev;
    if (delta <= 0) continue;

    const deltaUsd = price != null ? delta * price : null;

    // Kept whether or not this burn is worth alerting on: its value is as context
    // for a listing that shows up minutes later. Anyone can burn for any mint, so
    // this is the only defensible link between the two — closeness in time.
    state.recentBurns.push({ wallet, delta, usd: deltaUsd, at: new Date().toISOString() });
    if (state.recentBurns.length > 20) state.recentBurns = state.recentBurns.slice(-20);
    const crossed = [];
    if (Number.isFinite(gold) && prev < gold && total >= gold) crossed.push('🥇 GOLD');
    if (Number.isFinite(diamond) && prev < diamond && total >= diamond) crossed.push('💎 DIAMOND');

    // A threshold crossing is the whole point, so it outranks the dust filter.
    if (!crossed.length && deltaUsd != null && deltaUsd < CFG.burnMinUsd) continue;

    const lines = [
      `<b>🔥 $ANSEM BURN</b>`,
      '',
      `Burned: <b>${num(delta, 2)} ANSEM</b>${deltaUsd != null ? ` (~${money(deltaUsd)})` : ''}`,
      `Wallet total: ${num(total, 2)} ANSEM`,
      `<code>${esc(wallet)}</code>`,
    ];
    if (crossed.length) {
      lines.push('', `<b>Crosses the ${crossed.join(' and ')} threshold.</b>`);
    } else if (Number.isFinite(gold) && total < gold) {
      lines.push('', `${num(gold - total, 0)} ANSEM short of Gold (${num(gold, 0)}).`);
    }

    // Deliberately no coin named here. The Get Listed flow is "paste a mint, then
    // burn", so the burner need not own or have created the coin being listed —
    // nothing on chain or in this API ties the two together. Naming a guess would
    // be worse than naming nothing, because it is actionable and wrong. The coin
    // becomes knowable when it shows up in the feed, and that alert carries the
    // burn back as context.
    lines.push('', state.listingEnabled === true
      ? `<i>Listings are open — if this paid a listing fee, the coin should ` +
        `appear in the feed shortly and you'll get a separate alert naming it.</i>`
      : `<i>Which coin this is for is not knowable from the burn alone. ` +
        `Watch for the tier or listing alert that follows — that one names it.</i>`);

    notices.push(lines.join('\n'));
  }
  return notices;
}

/**
 * Listing availability, tier thresholds and the site-wide gate.
 *
 * Thresholds are read rather than hardcoded on purpose: /burn renders
 * 25,000/100,000 while /api/config returns 92,627/370,508 for the same tiers,
 * because the API recomputes them from the live $ANSEM price. Pinning either
 * number would have gone stale within a day.
 */
async function refreshStatus(state, isFirstRun) {
  const notices = [];

  const cfg = await fetchJson('/config').catch(err => {
    vlog(`config: ${err.message}`); return null;
  });
  if (cfg?.tierThresholds) {
    state.tierThresholds = cfg.tierThresholds;
    const p = Number(cfg.tierThresholds.ansemPriceUsd);
    if (Number.isFinite(p) && p > 0) state.ansemPriceUsd = p;
  }

  const listing = await fetchJson('/listing/config').catch(err => {
    vlog(`listing/config: ${err.message}`); return null;
  });
  if (listing && typeof listing.enabled === 'boolean') {
    const was = state.listingEnabled;
    state.listingEnabled = listing.enabled;
    if (!isFirstRun && was !== null && was !== listing.enabled) {
      notices.push(listing.enabled
        ? `<b>🟩 LISTINGS ARE OPEN</b>\n\n` +
          `ansem.io is accepting paid listings again.\n` +
          `Fee: <b>${money(listing.usdAmount)}</b>` +
          (Number.isFinite(Number(listing.ansemPriceUsd)) && Number(listing.usdAmount) > 0
            ? ` (~${num(Number(listing.usdAmount) / Number(listing.ansemPriceUsd), 0)} ANSEM)`
            : '') + '\n' +
          `Burn: ${listing.burnAvailable ? 'yes' : 'no'}   ` +
          `Airdrop route: ${listing.airdropAvailable ? 'yes' : 'no'}\n\n` +
          `New listings should start appearing in the feed.`
        : `<b>🟥 Listings paused</b>\n\nansem.io stopped accepting paid listings.`);
    }
  }

  const gate = await fetchJson('/gate').catch(err => {
    vlog(`gate: ${err.message}`); return null;
  });
  if (gate && typeof gate === 'object') {
    const now = JSON.stringify({ mode: gate.mode ?? null, launchAt: gate.launchAt ?? null });
    const was = state.gate;
    state.gate = now;
    if (!isFirstRun && was && was !== now) {
      notices.push(
        `<b>⏳ Site gate changed</b>\n\n` +
        `Was: <code>${esc(was)}</code>\nNow: <code>${esc(now)}</code>\n\n` +
        `This is the countdown that gates the whole site.`);
    }
  }

  return notices;
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
  return { gold: '🥇 GOLD', diamond: '💎 DIAMOND', free: '⚪ FREE' }[tier] ?? String(tier).toUpperCase();
}

// ------------------------------------------------------- paid-listing detection

const KNOWN_TIERS = ['free', 'gold', 'diamond'];
const BASELINE_STATUSES = ['on_curve', 'migrated'];

/**
 * A launchpad coin is born on a bonding curve, so it carries a `curvePct`. The
 * "Get Listed" flow instead takes an *existing* Solana token — the site reads its
 * name, price and market cap from DexScreener — so it never has a curve.
 *
 * Every one of the 200 coins in the live feed had a non-null `curvePct`, so this
 * currently matches nothing. That is deliberate: it stays silent until listings
 * reopen, rather than guessing and firing on launchpad coins. The schema-drift
 * alert below is the backstop if a real listing turns out to look different.
 */
function isPaidListing(coin) {
  return coin.curvePct == null && String(coin.status ?? '') !== 'on_curve';
}

// How far back a burn can be and still plausibly be this listing's payment.
const BURN_LOOKBACK_MS = 60 * 60 * 1000;

function recentBurnLines(state) {
  const cutoff = Date.now() - BURN_LOOKBACK_MS;
  const recent = (state?.recentBurns ?? [])
    .filter(b => Date.parse(b.at) >= cutoff)
    .slice(-3).reverse();
  if (!recent.length) return [];
  return ['', `<b>Burns in the last hour</b> — one of these likely paid for it:`,
    ...recent.map(b =>
      `· ${num(b.delta, 0)} ANSEM${b.usd != null ? ` (~${money(b.usd)})` : ''}\n` +
      `  <code>${esc(b.wallet)}</code>`)];
}

function buildMessage(coin, { upgraded, listing }, state) {
  const url = `${CFG.siteUrl}/launch/coin/${coin.mint}`;
  const headline = listing
    ? `💰 PAID LISTING — ${tierBadge(coin.tier)}`
    : upgraded
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
  if (listing) lines.push(...recentBurnLines(state));
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
  const listings = CFG.watchListings ? coins.filter(c => c.mint && isPaidListing(c)) : [];
  vlog(`fetched ${coins.length} coins, ${wanted.length} in [${CFG.tiers.join(', ')}]` +
       (CFG.watchListings ? `, ${listings.length} off-curve` : ''));

  const isFirstRun = state.runs === 0;
  const fresh = [];
  const notices = [];

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
    fresh.push({ coin, key, upgraded, listing: false });
  }

  // A coin can qualify on both axes at once — a $25k burn that also lifts it into
  // Gold. Record the listing either way so it cannot fire later, but only send
  // one message for it.
  const alreadyAlerted = new Set(fresh.map(f => f.coin.mint));
  for (const coin of listings) {
    const key = `listing:${coin.mint}`;
    if (state.seen[key]) continue;
    state.seen[key] = {
      tier: String(coin.tier ?? '').toLowerCase(),
      ticker: coin.ticker ?? null,
      firstAlertedAt: new Date().toISOString(),
    };
    if (alreadyAlerted.has(coin.mint)) continue;
    fresh.push({ coin, key, upgraded: false, listing: true });
  }

  // --- schema drift ---------------------------------------------------------
  // Everything here rides on an undocumented endpoint whose paid-listing shape we
  // have never actually seen. A value outside the known set means either the
  // schema moved or a new product surface appeared — both are reasons to look.
  const seenStatuses = [...new Set(coins.map(c => c.status).filter(Boolean).map(String))];
  const baseline = state.knownStatuses.length ? state.knownStatuses : BASELINE_STATUSES;
  const newStatuses = seenStatuses.filter(s => !baseline.includes(s));
  const newTiers = [...new Set(coins.map(c => String(c.tier ?? '').toLowerCase()).filter(Boolean))]
    .filter(t => !KNOWN_TIERS.includes(t));

  state.knownStatuses = [...new Set([...baseline, ...seenStatuses])];

  if (CFG.schemaDriftAlert && !isFirstRun && (newStatuses.length || newTiers.length)) {
    const bits = [];
    if (newStatuses.length) bits.push(`status: <code>${esc(newStatuses.join(', '))}</code>`);
    if (newTiers.length) bits.push(`tier: <code>${esc(newTiers.join(', '))}</code>`);
    notices.push(
      `<b>⚠️ ansem.io feed changed shape</b>\n\n` +
      `Value(s) never seen before —\n${bits.join('\n')}\n\n` +
      `Paid listings were expected to look different from curve launches, so this ` +
      `may be the first one. Worth checking whether the filter still matches.`,
    );
  }

  // --- side channels --------------------------------------------------------
  // Status first, so a burn is measured against fresh thresholds. Both are
  // wrapped: these endpoints were mapped from the browser, not verified here, so
  // a shape change must degrade to a log line rather than take down the watcher.
  if (CFG.watchStatus && (isFirstRun || state.runs % CFG.statusEvery === 0)) {
    try {
      notices.push(...await refreshStatus(state, isFirstRun));
    } catch (err) {
      vlog(`status channels unavailable: ${err.message}`);
    }
  }

  if (CFG.watchBurns) {
    try {
      notices.push(...await checkBurns(state, isFirstRun));
    } catch (err) {
      vlog(`burn channel unavailable: ${err.message}`);
    }
  }

  state.runs += 1;
  state.lastRunAt = new Date().toISOString();
  state.lastOkAt = new Date().toISOString();

  // Persist BEFORE sending. If Telegram is down we would rather miss one alert
  // than replay every alert on the next tick.
  await saveState(state);

  if (isFirstRun && !CFG.alertOnFirstRun) {
    log(`first run: recorded ${fresh.length} existing ${CFG.tiers.join('/')}/listing entr(ies) ` +
        `without alerting (set ALERT_ON_FIRST_RUN=true to change this)`);
    return 0;
  }

  for (const text of notices) {
    const label = text.match(/<b>(.*?)<\/b>/)?.[1] ?? 'notice';
    try {
      await sendTelegram(text);
      log(`alerted: ${label}`);
    } catch (err) {
      // No rollback. Every notice source (drift, burns, status) has already
      // absorbed the change into state, so each fires once at most either way.
      // Losing one is better than looping on it forever.
      log(`ERROR sending notice (${label}): ${err.message}`);
    }
    await sleep(1_200);
  }

  for (const item of fresh) {
    try {
      await sendTelegram(buildMessage(item.coin, item, state));
      const kind = item.listing ? ' [paid listing]' : item.upgraded ? ' [upgrade]' : '';
      log(`alerted: ${item.coin.ticker} (${item.coin.tier})${kind}`);
    } catch (err) {
      log(`ERROR sending alert for ${item.coin.ticker}: ${err.message}`);
      // Roll back just this key so the next tick retries it.
      delete state.seen[item.key];
      await saveState(state);
    }
    await sleep(1_200); // stay under Telegram's ~30 msg/sec and per-chat limits
  }
  return fresh.length;
}

// ------------------------------------------------- liveness (silence ≠ healthy)
//
// The failure mode that costs you the alert is not a crash — it is the loop
// staying up while every fetch is refused. Cloudflare fronts this endpoint and
// challenges a fraction of requests; if that fraction ever goes to 1, the logs
// fill with "ERROR during check" and Telegram stays quiet, which is exactly what
// a genuinely quiet feed looks like. These three make the difference visible.

const since = (iso) => (iso ? Date.now() - Date.parse(iso) : Infinity);
const mins = (ms) => (Number.isFinite(ms) ? `${Math.round(ms / 60_000)}m` : 'ever');

async function maybeStaleAlert(state, err) {
  const stale = since(state.lastOkAt);
  if (stale < CFG.staleAlertMs) return;
  if (since(state.staleAlertedAt) < CFG.staleAlertMs) return; // don't nag every tick

  state.staleAlertedAt = new Date().toISOString();
  await saveState(state);
  try {
    await sendTelegram(
      `<b>🔴 ansem-tier-watch is blind</b>\n\n` +
      `No successful fetch in <b>${mins(stale)}</b>.\n` +
      `Last error: <code>${esc(String(err.message).slice(0, 200))}</code>\n\n` +
      `The feed holds only ~50 minutes of history, so coins that appear and age ` +
      `out during an outage are missed permanently. Cloudflare blocking is the ` +
      `likely cause — check the logs, or run <code>npm run inspect</code>.`,
    );
    log(`alerted: stale for ${mins(stale)}`);
  } catch (e) {
    // Telegram may be down too. The next tick will try again.
    log(`ERROR sending stale alert: ${e.message}`);
  }
}

async function announceRecovery(state) {
  if (!state.staleAlertedAt) return;
  state.staleAlertedAt = null;
  await saveState(state);
  try {
    await sendTelegram(`<b>🟢 ansem-tier-watch recovered</b>\n\nFetching normally again.`);
    log('alerted: recovered');
  } catch (e) {
    log(`ERROR sending recovery alert: ${e.message}`);
  }
}

async function maybeHeartbeat(state) {
  if (!CFG.heartbeatMs) return;
  if (since(state.lastHeartbeatAt) < CFG.heartbeatMs) return;

  const first = !state.lastHeartbeatAt;
  state.lastHeartbeatAt = new Date().toISOString();
  await saveState(state);
  if (first) return; // don't fire one the moment the disk is provisioned

  const tiered = Object.keys(state.seen).filter(k => !k.startsWith('listing:')).length;
  const listed = Object.keys(state.seen).filter(k => k.startsWith('listing:')).length;
  try {
    await sendTelegram(
      `<b>💚 ansem-tier-watch alive</b>\n\n` +
      `Watching: ${esc(CFG.tiers.join('/'))}${CFG.watchListings ? ' + paid listings' : ''}\n` +
      `Recorded so far: ${tiered} tier hit(s), ${listed} listing(s)\n` +
      `Checks completed: ${state.runs}`,
    );
    log('alerted: heartbeat');
  } catch (e) {
    log(`ERROR sending heartbeat: ${e.message}`);
  }
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

  // A fresh state file — first deploy, or a wiped disk — has no lastOkAt, which
  // reads as "never succeeded" and would fire the blind alarm off a single failed
  // cycle seconds after boot. Start the clock at process start instead, so the
  // alarm still requires a real STALE_ALERT_MS of failure.
  state.lastOkAt ??= new Date().toISOString();

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
      await announceRecovery(state);
      await maybeHeartbeat(state);
    } catch (err) {
      log(`ERROR during check: ${err.message}`);
      await maybeStaleAlert(state, err);
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
