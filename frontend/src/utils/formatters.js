/**
 * Format a number as USD currency.
 */
export function formatUSD(value) {
  if (value == null || value === 'N/A') return 'N/A';
  const num = Number(value);
  if (isNaN(num)) return 'N/A';
  if (num >= 1_000_000) return `$${(num / 1_000_000).toFixed(2)}M`;
  if (num >= 1_000) return `$${(num / 1_000).toFixed(1)}K`;
  if (num < 0.01) return `$${num.toFixed(6)}`;
  return `$${num.toFixed(2)}`;
}

/**
 * Format a percentage value.
 */
export function formatPct(value) {
  if (value == null || value === 'N/A') return 'N/A';
  const num = Number(value);
  if (isNaN(num)) return 'N/A';
  return `${num.toFixed(1)}%`;
}

/**
 * Shorten a blockchain address.
 */
export function shortAddress(addr) {
  if (!addr || addr.length < 10) return addr || 'N/A';
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

/**
 * Format a date string to a readable local format.
 */
export function formatDate(isoString) {
  if (!isoString) return 'N/A';
  const d = new Date(isoString);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/**
 * Get the CSS color class for a score.
 */
export function scoreColor(score) {
  if (score >= 70) return '#ff4444';
  if (score >= 50) return '#ffaa00';
  return '#666';
}

/**
 * Get confidence badge color.
 */
export function confidenceColor(level) {
  switch (level) {
    case 'HIGH': return '#ff4444';
    case 'MEDIUM': return '#ffaa00';
    case 'LOW': return '#666';
    default: return '#444';
  }
}
