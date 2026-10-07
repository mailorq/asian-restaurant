// a session lasts while one identity is signed in; the requests and mutations started in it belong to it
let session = 0;

export function currentSession(): number {
  return session;
}

export function endSession(): void {
  session += 1;
}
