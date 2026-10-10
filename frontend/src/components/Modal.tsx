import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(panel: HTMLElement): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);
}

export function Modal({
  title,
  back,
  onClose,
  children,
  footer,
}: {
  title: string;
  // a step back inside the dialog, shown before the title and kept out of the dialog's name
  back?: { label: string; onClick: () => void };
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  // the dialog lives outside the app root, so the page behind it is made inert: neither tab nor a screen
  // reader reaches it. focus starts in the dialog and goes back to whatever opened it
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const root = document.getElementById("root");
    if (root) root.inert = true;
    panelRef.current?.focus({ preventScroll: true });
    return () => {
      if (root) root.inert = false;
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);

  // a step of the dialog replaces the control that led to it; focus stays in the dialog instead of falling out
  useEffect(() => {
    const panel = panelRef.current;
    if (panel && !panel.contains(document.activeElement)) panel.focus({ preventScroll: true });
  }, [title]);

  function keepFocus(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Tab" || !panelRef.current) return;
    const items = focusables(panelRef.current);
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (!first) {
      e.preventDefault();
    } else if (e.shiftKey && (active === first || active === panelRef.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <div
      className="anim-fade-in fixed inset-0 z-[100] flex items-end justify-center bg-black/55 p-0 backdrop-blur-[3px] sm:items-center sm:p-6"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={keepFocus}
        className="anim-sheet flex max-h-[92dvh] w-full max-w-lg flex-col overflow-hidden rounded-t-3xl border border-border bg-surface shadow-lg outline-none sm:rounded-3xl"
        onClick={(e) => e.stopPropagation()}
      >
        <span aria-hidden className="mx-auto mt-2.5 h-1 w-10 rounded-full bg-border sm:hidden" />
        <div className="flex items-center justify-between gap-4 px-6 pb-3 pt-3 sm:pt-5">
          <div className="flex min-w-0 items-center gap-1">
            {back && (
              <button onClick={back.onClick} aria-label={back.label} className="icon-btn -ml-2.5 shrink-0">
                <Icon name="arrowLeft" size={20} />
              </button>
            )}
            <h2 id={titleId} className="font-display text-2xl font-semibold leading-tight">
              {title}
            </h2>
          </div>
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
    </div>,
    document.body,
  );
}
