// the server's rule for a new login, checked on what it stores: the lowercased input. no i or u flag, so
// [a-z] stays the latin letters
const LOGIN = /^[a-z][a-z0-9_.-]{2,31}$/;

export function isValidLogin(raw: string): boolean {
  return LOGIN.test(raw.trim().toLowerCase());
}
