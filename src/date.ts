const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

// Emula `datetime.strftime` para templates (UTC, como datetime.utcnow).
export function pyDate(value: string | Date | null | undefined): {
  strftime(fmt: string): string;
  toString(): string;
} {
  const d = value ? new Date(value) : new Date();
  const strftime = (fmt: string): string =>
    fmt
      .replace(/%Y/g, String(d.getUTCFullYear()))
      .replace(/%m/g, pad(d.getUTCMonth() + 1))
      .replace(/%d/g, pad(d.getUTCDate()))
      .replace(/%H/g, pad(d.getUTCHours()))
      .replace(/%M/g, pad(d.getUTCMinutes()))
      .replace(/%S/g, pad(d.getUTCSeconds()))
      .replace(/%b/g, MONTHS[d.getUTCMonth()] ?? '')
      .replace(/%f/g, String(d.getUTCMilliseconds() * 1000).padStart(6, '0'))
      .replace(/%j/g, String(Math.floor((d.getTime() - Date.UTC(d.getUTCFullYear(), 0, 0)) / 86400000)).padStart(3, '0'))
      .replace(/%p/g, d.getUTCHours() < 12 ? 'AM' : 'PM')
      .replace(/%I/g, (() => {
        const h = d.getUTCHours() % 12 || 12;
        return pad(h);
      })());
  return { strftime, toString: () => d.toISOString() };
}
