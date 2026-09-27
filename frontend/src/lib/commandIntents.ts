export type CommandState = "pending" | "dispatched" | "succeeded" | "rejected" | "timed_out" | "dispatch_failed";

export interface Command {
  command_id: string;
  status: CommandState;
  result_code: string;
  result_detail: string;
  deadline_at: string | null;
  created_at: string;
}

export interface Transition {
  orderId: number;
  expected_status: string;
  to_status: string;
  note?: string;
}

// one action of an employee under one key, stored until its command has a final state or was refused outright
export interface Intent extends Transition {
  key: string;
  commandId?: string;
  state?: CommandState;
  resultCode?: string;
}

export interface Store {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface Deps {
  send(transition: Transition, key: string): Promise<Command>;
  read(commandId: string): Promise<Command>;
  store: Store;
  sleep(ms: number): Promise<void>;
  newKey(): string;
  now(): number;
}

// timed_out and dispatch_failed wait for an operator or a late outcome, only these two end a command
const FINAL: CommandState[] = ["succeeded", "rejected"];
const REPEATS = 3;
const WATCH_MS = 20_000;

export class OutcomeUnknown extends Error {
  constructor(readonly intent: Intent) {
    super("Исход неизвестен. Действие сохранено, его можно продолжить тем же ключом");
  }
}

// no answer, operations not reached, or the reply lost: the command may or may not exist
function outcomeOpen(e: unknown): boolean {
  const status = (e as { status?: number }).status;
  return status === 0 || status === 503 || status === 504;
}

export function modeChanged(e: unknown): boolean {
  const failure = e as { status?: number; body?: { code?: string } };
  return failure.status === 403 && failure.body?.code === "transition_mode_changed";
}

const storeKey = (userId: number) => `pending-commands:${userId}`;

export function pendingIntents(store: Store, userId: number): Intent[] {
  try {
    const kept = JSON.parse(store.getItem(storeKey(userId)) ?? "[]");
    return Array.isArray(kept) ? kept : [];
  } catch {
    return [];
  }
}

function save(store: Store, userId: number, intent: Intent): void {
  const rest = pendingIntents(store, userId).filter((kept) => kept.key !== intent.key);
  store.setItem(storeKey(userId), JSON.stringify([...rest, intent]));
}

function forget(store: Store, userId: number, key: string): void {
  const rest = pendingIntents(store, userId).filter((kept) => kept.key !== key);
  if (rest.length) store.setItem(storeKey(userId), JSON.stringify(rest));
  else store.removeItem(storeKey(userId));
}

function applied(intent: Intent, command: Command): Intent {
  return { ...intent, commandId: command.command_id, state: command.status, resultCode: command.result_code };
}

async function watch(deps: Deps, userId: number, intent: Intent): Promise<Intent> {
  const until = deps.now() + WATCH_MS;
  let current = intent;
  while (!FINAL.includes(current.state!) && deps.now() < until) {
    await deps.sleep(1000);
    try {
      current = applied(current, await deps.read(current.commandId!));
    } catch {
      break;
    }
    save(deps.store, userId, current);
  }
  if (FINAL.includes(current.state!)) forget(deps.store, userId, current.key);
  return current;
}

async function drive(deps: Deps, userId: number, intent: Intent): Promise<Intent> {
  if (intent.commandId) {
    try {
      const refreshed = applied(intent, await deps.read(intent.commandId));
      save(deps.store, userId, refreshed);
      return await watch(deps, userId, refreshed);
    } catch (e) {
      if (outcomeOpen(e)) return intent;
      throw e;
    }
  }
  for (let attempt = 0; ; attempt++) {
    try {
      const sent = applied(intent, await deps.send(intent, intent.key));
      save(deps.store, userId, sent);
      return await watch(deps, userId, sent);
    } catch (e) {
      if (!outcomeOpen(e)) {
        forget(deps.store, userId, intent.key);
        throw e;
      }
      if (attempt + 1 >= REPEATS) throw new OutcomeUnknown(intent);
      await deps.sleep(1000 * 2 ** attempt);
    }
  }
}

// the same action clicked again continues the stored one instead of minting a second key
export async function submit(deps: Deps, userId: number, transition: Transition): Promise<Intent> {
  const same = pendingIntents(deps.store, userId).find(
    (kept) =>
      kept.orderId === transition.orderId &&
      kept.expected_status === transition.expected_status &&
      kept.to_status === transition.to_status,
  );
  const intent = same ?? { ...transition, key: deps.newKey() };
  save(deps.store, userId, intent);
  return drive(deps, userId, intent);
}

export async function resume(deps: Deps, userId: number, key: string): Promise<Intent> {
  const intent = pendingIntents(deps.store, userId).find((kept) => kept.key === key);
  if (!intent) throw new Error("Действие уже завершено");
  return drive(deps, userId, intent);
}

export function dismiss(store: Store, userId: number, key: string): void {
  forget(store, userId, key);
}
