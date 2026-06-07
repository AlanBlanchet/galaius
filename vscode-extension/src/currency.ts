// Display-currency conversion for the dashboard's spend figures. Usage cost is recorded in USD
// (litellm pricing); this converts it to the user's `interact.display.currency` for display only.
// Rates are fetched LIVE from the ECB via frankfurter.app (no key, no hardcoded table — they
// update when the ECB updates) and cached; formatting uses the built-in Intl currency formatter.
// Offline / unknown currency falls back to USD so the panel never breaks.

let _cache: { target: string; rate: number; at: number } | undefined;
const TTL_MS = 60 * 60 * 1000; // rates move slowly; an hour is plenty and keeps it offline-friendly

/** USD→target rate (1 for USD or on any failure). Cached for an hour per target. */
export async function usdRateTo(target: string): Promise<number> {
  const cur = (target || "USD").toUpperCase();
  if (cur === "USD") return 1;
  if (_cache && _cache.target === cur && Date.now() - _cache.at < TTL_MS) {
    return _cache.rate;
  }
  try {
    const res = await fetch(
      `https://api.frankfurter.app/latest?from=USD&to=${encodeURIComponent(cur)}`,
    );
    const json = (await res.json()) as { rates?: Record<string, number> };
    const rate = json.rates?.[cur];
    if (typeof rate === "number" && rate > 0) {
      _cache = { target: cur, rate, at: Date.now() };
      return rate;
    }
  } catch {
    // offline or rate host down — fall back to USD (1:1) rather than failing the panel.
  }
  return 1;
}

/** Format a USD amount in `target` currency at `rate`, via Intl (locale-aware symbol). */
export function formatMoney(usd: number, target: string, rate: number): string {
  const cur = (target || "USD").toUpperCase();
  const amount = usd * rate;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: cur,
      maximumFractionDigits: Math.abs(amount) < 1 ? 4 : 2,
    }).format(amount);
  } catch {
    return `$${amount.toFixed(2)}`; // unknown currency code
  }
}

/** The currency symbol for `target` (for chart axis prefixes), e.g. "$", "€", "£". */
export function currencySymbol(target: string): string {
  const cur = (target || "USD").toUpperCase();
  try {
    const parts = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: cur,
    }).formatToParts(0);
    return parts.find((p) => p.type === "currency")?.value ?? "$";
  } catch {
    return "$";
  }
}

/** Currencies offered in the picker — common ones; rates for any ISO-4217 code still work. */
export const COMMON_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR"];
