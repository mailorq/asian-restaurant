import { useState } from "react";
import { Icon } from "./Icon";
import { useAuth } from "../stores/auth";

// the tab could not find out who is signed in: until it does it shows neither a guest nor anyone else
export function AuthCheckNotice() {
  const failed = useAuth((s) => s.failed);
  const refresh = useAuth((s) => s.refresh);
  const [checking, setChecking] = useState(false);

  if (!failed) return null;

  async function retry() {
    setChecking(true);
    await refresh();
    setChecking(false);
  }

  return (
    <div
      role="alert"
      className="anim-fade-up fixed inset-x-4 bottom-[max(1rem,env(safe-area-inset-bottom))] z-[90] mx-auto flex max-w-lg items-center gap-3 rounded-2xl border border-border bg-surface py-2.5 pl-4 pr-2.5 text-sm shadow-lg"
    >
      <Icon name="user" size={18} className="shrink-0 text-danger" />
      <span className="min-w-0 flex-1">Не удалось проверить вход. Проверьте соединение</span>
      <button onClick={retry} disabled={checking} className="btn btn-secondary h-9 min-h-9 shrink-0 px-4 text-sm">
        {checking ? "Проверяем" : "Повторить"}
      </button>
    </div>
  );
}
