import { useState } from "react";
import { Icon } from "./Icon";
import { SearchBox } from "./SearchBox";
import { ThemeToggle } from "./ThemeToggle";
import { UserMenu } from "./UserMenu";
import { useUI, type View } from "../stores/ui";
import { useCartQuery } from "../api/cart";
import { useAuth } from "../stores/auth";
import { formatUaPhone } from "../lib/phone";

const NAV: { label: string; view: View }[] = [
  { label: "Главная", view: { name: "home" } },
  { label: "Меню", view: { name: "menu" } },
];

export function Header() {
  const view = useUI((s) => s.view);
  const navigate = useUI((s) => s.navigate);
  const openModal = useUI((s) => s.openModal);
  const count = useCartQuery().data?.count ?? 0;
  const user = useAuth((s) => s.user);
  const logout = useAuth((s) => s.logout);
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-bg/85 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-4 px-4 sm:px-6">
        <button
          onClick={() => navigate({ name: "home" })}
          className="shrink-0 font-display text-xl font-bold tracking-tight sm:text-2xl"
        >
          Asian<span className="text-accent">.</span>
        </button>

        <nav className="ml-2 hidden items-center gap-1 md:flex">
          {NAV.map((item) => {
            const active = item.view.name === view.name;
            return (
              <button
                key={item.label}
                onClick={() => navigate(item.view)}
                aria-current={active ? "page" : undefined}
                className={`relative rounded-full px-3.5 py-2 text-sm font-medium transition-colors ${
                  active ? "text-text" : "text-muted hover:text-text"
                }`}
              >
                {item.label}
                {active && (
                  <span className="absolute inset-x-0 -bottom-0.5 mx-auto h-0.5 w-4 rounded-full bg-accent" />
                )}
              </button>
            );
          })}
        </nav>

        <div className="ml-auto hidden lg:block">
          <SearchBox />
        </div>

        <div className="ml-auto flex items-center gap-0.5 lg:ml-3">
          <ThemeToggle />
          {user?.is_employee && (
            <button
              onClick={() => navigate({ name: "employee" })}
              className="btn btn-secondary mx-1 hidden h-10 min-h-10 px-3.5 text-sm text-muted sm:inline-flex"
            >
              <Icon name="bowl" size={16} /> Панель
            </button>
          )}
          <button
            onClick={() => navigate({ name: "orders" })}
            aria-label="Мои заказы"
            className="icon-btn hidden sm:inline-grid"
          >
            <Icon name="clock" size={20} />
          </button>
          <button onClick={() => openModal("cart")} aria-label="Корзина" className="icon-btn relative">
            <Icon name="cart" size={20} />
            {count > 0 && (
              // keyed by the count, so it answers every change with a short pop
              <span
                key={count}
                className="anim-scale-in tnum absolute right-0.5 top-0.5 grid h-5 min-w-5 place-items-center rounded-full bg-accent px-1 text-[11px] font-bold text-accent-contrast"
              >
                {count}
              </span>
            )}
          </button>
          {user ? (
            <UserMenu />
          ) : (
            <button
              onClick={() => openModal("auth", "login")}
              className="btn btn-secondary ml-1.5 hidden h-10 min-h-10 px-4 text-sm sm:inline-flex"
            >
              Войти
            </button>
          )}
          <button
            onClick={() => setMobileOpen((v) => !v)}
            aria-label="Меню"
            aria-expanded={mobileOpen}
            className="icon-btn md:hidden"
          >
            <Icon name={mobileOpen ? "close" : "menu"} size={22} />
          </button>
        </div>
      </div>

      {mobileOpen && (
        <div className="anim-fade-in border-t border-border bg-bg px-4 pb-4 pt-3 shadow-md md:hidden">
          <div className="mb-3 lg:hidden">
            <SearchBox />
          </div>
          <div className="flex flex-col">
            {[
              ...NAV,
              { label: "Мои заказы", view: { name: "orders" } as View },
              ...(user?.is_employee ? [{ label: "Панель сотрудника", view: { name: "employee" } as View }] : []),
            ].map((item) => (
              <button
                key={item.label}
                onClick={() => {
                  navigate(item.view);
                  setMobileOpen(false);
                }}
                aria-current={item.view.name === view.name ? "page" : undefined}
                className={`flex h-12 items-center rounded-xl px-3 text-left text-base font-medium hover:bg-surface-2 ${
                  item.view.name === view.name ? "text-text" : "text-muted"
                }`}
              >
                {item.label}
              </button>
            ))}
            <div className="my-2 h-px bg-border" />
            {user ? (
              <button
                onClick={() => {
                  logout();
                  setMobileOpen(false);
                }}
                className="flex h-12 items-center gap-2.5 rounded-xl px-3 text-left text-base font-medium text-danger hover:bg-danger/10"
              >
                <Icon name="logout" size={18} /> Выйти ({user.name || formatUaPhone(user.phone ?? "")})
              </button>
            ) : (
              <button
                onClick={() => {
                  openModal("auth", "login");
                  setMobileOpen(false);
                }}
                className="btn btn-primary mt-1 h-12"
              >
                Войти в аккаунт
              </button>
            )}
          </div>
        </div>
      )}
    </header>
  );
}
