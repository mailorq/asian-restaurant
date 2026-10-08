import { useEffect, type ReactNode } from "react";
import { Icon } from "./Icon";

export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  return (
    <div
      className="anim-fade-in fixed inset-0 z-[100] flex items-end justify-center bg-black/55 p-0 backdrop-blur-[3px] sm:items-center sm:p-6"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="anim-sheet flex max-h-[92dvh] w-full max-w-lg flex-col overflow-hidden rounded-t-3xl border border-border bg-surface shadow-lg sm:rounded-3xl"
        onClick={(e) => e.stopPropagation()}
      >
        <span aria-hidden className="mx-auto mt-2.5 h-1 w-10 rounded-full bg-border sm:hidden" />
        <div className="flex items-center justify-between gap-4 px-6 pb-3 pt-3 sm:pt-5">
          <h2 className="font-display text-2xl font-semibold leading-tight">{title}</h2>
          <button onClick={onClose} aria-label="Закрыть" className="icon-btn -mr-2 shrink-0">
            <Icon name="close" size={20} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 pb-6 pt-2">{children}</div>

        {footer && (
          <div className="border-t border-border bg-surface px-6 pb-[max(1.25rem,env(safe-area-inset-bottom))] pt-4">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
