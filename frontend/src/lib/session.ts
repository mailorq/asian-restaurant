// a session lasts while one identity is signed in; the requests and mutations started in it belong to it
let session = 0;
// undefined until the server said who is signed in: an unknown account is not a guest
let account: number | null | undefined;
// settles once the account is known. when the tab could not find it out, what waits is refused instead of
// waiting for nothing, and the next request that needs the account makes the tab ask again
let known: Promise<void>;
let settle = { resolve: () => {}, reject: (_reason: unknown) => {} };
let failed = false;
let changed: () => void = () => {};
let reask: () => void = () => {};

function awaitAccount(): void {
  failed = false;
  known = new Promise((resolve, reject) => {
    settle = { resolve, reject };
  });
  // a refusal nobody waits for is not an error of its own
  known.catch(() => {});
}

awaitAccount();

export function currentSession(): number {
  return session;
}

export function endSession(): void {
  session += 1;
}

export function currentAccount(): number | null | undefined {
  return account;
}

export function setAccount(next: number | null | undefined): void {
  if (next === undefined) {
    if (account !== undefined) awaitAccount();
  } else {
    settle.resolve();
    if (failed) {
      failed = false;
      known = Promise.resolve();
    }
  }
  account = next;
}

// the tab asks who is signed in: what needs the account waits for this answer
export function askingAccount(): void {
  if (account === undefined && failed) awaitAccount();
}

// the tab could not find out who is signed in, so nothing waiting for the account is sent as anyone's
export function accountUnresolved(reason: unknown): void {
  if (account !== undefined) return;
  failed = true;
  settle.reject(reason);
}

// a request bound to an account waits for it, so it always says whose it is
export function accountKnown(): Promise<void> {
  if (failed) reask();
  return known;
}

export function onAccountWanted(handler: () => void): void {
  reask = handler;
}

// the server refused a request because the browser is signed in as someone else
export function onAccountChanged(handler: () => void): void {
  changed = handler;
}

export function accountChanged(): void {
  changed();
}
