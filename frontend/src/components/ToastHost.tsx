import { createPortal } from "react-dom";
import { Icon } from "./Icon";
import { useToast } from "../stores/toast";

export function ToastHost() {
  const toasts = useToast((s) => s.toasts);

  // next to the dialogs, outside the app root: an open dialog makes the root inert, and its notices
  // still have to be heard
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 top-4 z-[200] flex flex-col items-center gap-2 px-4">
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          aria-live="polite"
          className="anim-fade-up pointer-events-auto flex items-center gap-2.5 rounded-full border border-border bg-surface px-4 py-2.5 text-sm font-medium shadow-lg"
        >
          <span
            className={`grid h-5 w-5 place-items-center rounded-full ${
              t.tone === "success" ? "bg-accent text-accent-contrast" : "bg-danger text-white"
            }`}
          >
            <Icon name={t.tone === "success" ? "check" : "close"} size={13} strokeWidth={2.5} />
          </span>
          {t.message}
        </div>
      ))}
    </div>,
    document.body,
  );
}
