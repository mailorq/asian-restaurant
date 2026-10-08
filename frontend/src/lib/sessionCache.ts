import { useCallback } from "react";
import {
  useMutation,
  type DefaultError,
  type MutateOptions,
  type QueryClient,
  type UseMutationOptions,
  type UseMutationResult,
} from "@tanstack/react-query";

import { currentSession, endSession, setAccount } from "./session";

// every cached query belongs to the signed-in account: its orders, its last used address, its cart,
// the screens a staff member sees. when the identity changes - login, logout, register, a password
// reset, a refresh that sees a different account, another tab signing in - the whole cache is dropped
// and its in-flight fetches are detached, so the next account never reads the previous one's data and a
// slow answer to the previous account's request cannot land in the new cache. learning who is signed in
// (undefined is not known yet) changes nothing: no account was shown or sent for before it
export function onIdentityChange(
  qc: QueryClient,
  previousId: number | null | undefined,
  nextId: number | null | undefined,
): void {
  if (previousId === nextId) {
    return;
  }
  if (previousId !== undefined) {
    endSession();
    qc.clear();
  }
  setAccount(nextId);
}

// clearing the cache does not stop a mutation already on its way: its answer still arrives and its
// callbacks still run. each run is stamped with the session it started in, and its callbacks run only
// while that session lasts, so a late answer to the previous account neither writes the next one's
// cache nor speaks to them
export function sessionMutation<TData, TError, TVariables>(
  options: UseMutationOptions<TData, TError, TVariables, number>,
): UseMutationOptions<TData, TError, TVariables, number> {
  const { onSuccess, onError, onSettled } = options;
  return {
    ...options,
    onMutate: () => currentSession(),
    onSuccess:
      onSuccess &&
      ((data, variables, started, context) =>
        started === currentSession() ? onSuccess(data, variables, started, context) : undefined),
    onError:
      onError &&
      ((error, variables, started, context) =>
        started === currentSession() ? onError(error, variables, started, context) : undefined),
    onSettled:
      onSettled &&
      ((data, error, variables, started, context) =>
        started === currentSession() ? onSettled(data, error, variables, started, context) : undefined),
  };
}

// what a screen passes to mutate() gets the stamp of the run it belongs to and is held to it the same way
export function sessionCallbacks<TData, TError, TVariables>(
  callbacks: MutateOptions<TData, TError, TVariables, number>,
): MutateOptions<TData, TError, TVariables, number> {
  const { onSuccess, onError, onSettled } = callbacks;
  return {
    onSuccess:
      onSuccess &&
      ((data, variables, started, context) => {
        if (started === currentSession()) onSuccess(data, variables, started, context);
      }),
    onError:
      onError &&
      ((error, variables, started, context) => {
        if (started === currentSession()) onError(error, variables, started, context);
      }),
    onSettled:
      onSettled &&
      ((data, error, variables, started, context) => {
        if (started === currentSession()) onSettled(data, error, variables, started, context);
      }),
  };
}

export function useSessionMutation<TData = unknown, TError = DefaultError, TVariables = void>(
  options: UseMutationOptions<TData, TError, TVariables, number>,
): UseMutationResult<TData, TError, TVariables, number> {
  const mutation = useMutation(sessionMutation(options));
  const { mutate } = mutation;
  const guarded = useCallback(
    (variables: TVariables, callbacks?: MutateOptions<TData, TError, TVariables, number>) =>
      mutate(variables, callbacks && sessionCallbacks(callbacks)),
    [mutate],
  );
  return { ...mutation, mutate: guarded };
}
