import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { useAuth } from "../stores/auth";
import { formatUaPhone } from "../lib/phone";

export function UserMenu() {
  const user = useAuth((s) => s.user);
  const logout = useAuth((s) => s.logout);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!user) return null;
  const label = user.name || formatUaPhone(user.phone ?? "");

  return (
    <div
      ref={ref}
      className="relative ml-1 hidden sm:block"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="btn btn-secondary h-10 min-h-10 gap-2 px-3.5 text-sm text-muted hover:text-text"
      >
        <Icon name="user" size={16} />
        <span className="max-w-[130px] truncate">{label}</span>
        <Icon
          name="chevronDown"
          size={14}
          className={`transition-transform duration-200 ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open && (
        // pt-2 keeps the gap hoverable so the menu doesn't close while moving into it
        <div className="absolute right-0 top-full z-50 pt-2">
          <div
            role="menu"
            className="anim-fade-up w-56 overflow-hidden rounded-2xl border border-border bg-surface p-1.5 shadow-lg"
          >
            <div className="px-3 py-2">
              <p className="truncate text-sm font-medium">{label}</p>
              {user.name && user.phone && (
                <p className="tnum truncate text-xs text-muted">{formatUaPhone(user.phone)}</p>
              )}
            </div>
            <div className="my-1 h-px bg-border" />
            <button
              role="menuitem"
              onClick={() => {
                logout();
                setOpen(false);
              }}
              className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm font-medium text-danger transition-colors hover:bg-danger/10"
            >
              <Icon name="logout" size={17} /> Выйти
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
