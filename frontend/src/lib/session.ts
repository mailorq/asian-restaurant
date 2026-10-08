// a session lasts while one identity is signed in; the requests and mutations started in it belong to it
let session = 0;
// undefined until the server said who is signed in: an unknown account is not a guest
let account: number | null | undefined;
let known: Promise<void>;
let resolveKnown: () => void = () => {};
let changed: () => void = () => {};

function awaitAccount(): void {
  known = new Promise((resolve) => {
    resolveKnown = resolve;
  });
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
  if (next === undefined && account !== undefined) awaitAccount();
  if (next !== undefined) resolveKnown();
  account = next;
}

// a request bound to an account waits for it, so it always says whose it is
export function accountKnown(): Promise<void> {
  return known;
}

// the server refused a request because the browser is signed in as someone else
export function onAccountChanged(handler: () => void): void {
  changed = handler;
}

export function accountChanged(): void {
  changed();
}
