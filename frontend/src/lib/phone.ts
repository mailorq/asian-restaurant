// client-side helpers for ukrainian +380 numbers (covers ~99% of inputs).
// the backend re-validates every number with phonenumbers, this is ux only.

export function formatUaPhone(raw: string): string {
  let d = raw.replace(/\D/g, "");
  if (d.startsWith("380")) d = d.slice(3);
  d = d.slice(0, 9);
  let out = "+380";
  if (d.length > 0) out += ` (${d.slice(0, 2)}`;
  if (d.length >= 2) out += `) ${d.slice(2, 5)}`;
  if (d.length >= 5) out += ` ${d.slice(5, 7)}`;
  if (d.length >= 7) out += ` ${d.slice(7, 9)}`;
  return out;
}

export function isValidUaPhone(raw: string): boolean {
  return /^380\d{9}$/.test(raw.replace(/\D/g, ""));
}
