import { useId, useRef, useState } from "react";
import { Modal } from "./Modal";
import { Icon } from "./Icon";
import { useUI } from "../stores/ui";
import { useToast } from "../stores/toast";
import { useAuth, type CurrentUser } from "../stores/auth";
import { api, ApiError } from "../api/client";
import { formatUaPhone, isValidUaPhone } from "../lib/phone";

function Field({
  label,
  children,
  required,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  required?: boolean;
  hint?: string;
}) {
  return (
    <label className="block">
      <span className="field-label">
        {label}
        {required && <span className="text-accent"> *</span>}
      </span>
      {children}
      {hint && <span className="mt-1.5 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

function Spinner() {
  return <span aria-hidden className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

type Mode = "login" | "register";

const TABS = ["login", "register"] as const;

// the server's rule for a new login; it lowercases what was typed first
const LOGIN = /^[a-z][a-z0-9_.-]{2,31}$/;

export function AuthModal() {
  const mode = useUI((s) => s.authTab);
  const setTab = (t: Mode) => useUI.setState({ authTab: t });
  const close = useUI((s) => s.closeModal);
  const notify = useToast((s) => s.notify);
  const setUser = useAuth((s) => s.setUser);

  const [showPassword, setShowPassword] = useState(false);
  const [login, setLogin] = useState("");
  const [phone, setPhone] = useState("+380 ");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [phoneError, setPhoneError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const tabsId = useId();
  const tabRefs = useRef<Partial<Record<Mode, HTMLButtonElement | null>>>({});

  function switchTo(next: Mode) {
    setTab(next);
    setLoginError(null);
    setPhoneError(null);
    setError(null);
  }

  // arrows, home and end move between the tabs and choose the one they land on
  function moveTab(e: React.KeyboardEvent<HTMLDivElement>) {
    const at = TABS.indexOf(mode);
    const to =
      e.key === "ArrowRight"
        ? (at + 1) % TABS.length
        : e.key === "ArrowLeft"
          ? (at - 1 + TABS.length) % TABS.length
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? TABS.length - 1
              : null;
    if (to === null) return;
    e.preventDefault();
    switchTo(TABS[to]);
    tabRefs.current[TABS[to]]?.focus();
  }

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (mode === "register") {
      const loginOk = LOGIN.test(login.trim().toLowerCase());
      const phoneOk = isValidUaPhone(phone);
      setLoginError(loginOk ? null : "От 3 до 32 латинских букв, цифр и знаков _ . -, начинается с буквы");
      setPhoneError(phoneOk ? null : "Введите номер в формате +380 (XX) XXX XX XX");
      if (!loginOk || !phoneOk) return;
    }
    const [path, body, greeting] =
      mode === "login"
        ? ["/auth/login", { login, password }, "Вход выполнен"]
        : ["/auth/register", { login, password, name: name.trim(), phone }, "Добро пожаловать!"];
    setError(null);
    setLoading(true);
    try {
      const user = await api<CurrentUser>(path, { method: "POST", body: JSON.stringify(body) });
      setUser(user);
      notify(greeting);
      close();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Что-то пошло не так");
    } finally {
      setLoading(false);
    }
  }

  const title = mode === "login" ? "Вход" : "Регистрация";

  return (
    <Modal title={title} onClose={close}>
      <div
        role="tablist"
        aria-label="Вход или регистрация"
        onKeyDown={moveTab}
        className="relative mb-6 grid grid-cols-2 rounded-full bg-surface-2 p-1"
      >
        {/* the thumb slides to the chosen tab, so the switch shows where it went */}
        <span
          aria-hidden
          className={`absolute inset-y-1 left-1 w-[calc(50%-0.25rem)] rounded-full bg-surface shadow-sm transition-transform dark:bg-border duration-[var(--dur)] ease-[var(--ease-out)] ${
            mode === "register" ? "translate-x-full" : ""
          }`}
        />
        {TABS.map((t) => (
          <button
            key={t}
            ref={(el) => {
              tabRefs.current[t] = el;
            }}
            id={`${tabsId}-${t}`}
            role="tab"
            aria-selected={mode === t}
            aria-controls={`${tabsId}-panel`}
            tabIndex={mode === t ? 0 : -1}
            onClick={() => switchTo(t)}
            className={`relative h-10 rounded-full text-sm font-medium transition-colors ${
              mode === t ? "text-text" : "text-muted hover:text-text"
            }`}
          >
            {t === "login" ? "Вход" : "Регистрация"}
          </button>
        ))}
      </div>

      <div
        key={mode}
        id={`${tabsId}-panel`}
        role="tabpanel"
        aria-labelledby={`${tabsId}-${mode}`}
        className="anim-fade-up"
      >
        <form onSubmit={submit} className="flex flex-col gap-5">
          {mode === "register" && (
            <Field label="Имя" required>
              <input
                name="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="input"
                placeholder="Иван"
                autoComplete="name"
                required
                maxLength={150}
              />
            </Field>
          )}

          <Field label="Логин" required hint={mode === "login" ? "Если регистрировались по номеру телефона, введите номер" : undefined}>
            <input
              name="login"
              value={login}
              onChange={(e) => {
                setLogin(e.target.value);
                setLoginError(null);
              }}
              aria-invalid={loginError ? true : undefined}
              aria-describedby={loginError ? "login-error" : undefined}
              className="input"
              placeholder={mode === "login" ? "ivan_petrenko" : "Латиница, цифры, _ . -"}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              maxLength={mode === "login" ? 150 : 32}
            />
            {loginError && (
              <span id="login-error" role="alert" className="field-error block">
                {loginError}
              </span>
            )}
          </Field>

          {mode === "register" && (
            <Field label="Телефон для связи" required hint="По нему курьер свяжется с вами, для входа он не нужен">
              <input
                type="tel"
                inputMode="tel"
                value={phone}
                onChange={(e) => {
                  setPhone(formatUaPhone(e.target.value));
                  setPhoneError(null);
                }}
                aria-invalid={phoneError ? true : undefined}
                aria-describedby={phoneError ? "phone-error" : undefined}
                className="input"
                placeholder="+380 (67) 123 45 67"
                autoComplete="tel"
                required
              />
              {phoneError && (
                <span id="phone-error" role="alert" className="field-error block">
                  {phoneError}
                </span>
              )}
            </Field>
          )}

          <Field label="Пароль" required>
            <div className="relative">
              <input
                name="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                type={showPassword ? "text" : "password"}
                className="input pr-12"
                placeholder="••••••••"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                required
                minLength={8}
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? "Скрыть пароль" : "Показать пароль"}
                className="icon-btn absolute right-1 top-1/2 h-10 w-10 -translate-y-1/2"
              >
                <Icon name={showPassword ? "eyeOff" : "eye"} size={18} />
              </button>
            </div>
          </Field>

          {error && (
            <p role="alert" className="rounded-xl bg-danger/10 px-4 py-3 text-sm text-danger">
              {error}
            </p>
          )}

          <button type="submit" disabled={loading} className="btn btn-primary mt-2 h-12 w-full text-base">
            {loading && <Spinner />}
            {mode === "login" ? "Войти" : "Создать аккаунт"}
          </button>
        </form>

        {mode === "login" && (
          <p className="mt-4 text-center text-sm text-muted">
            Забыли пароль? Самостоятельно его не восстановить, обратитесь в ресторан.
          </p>
        )}

        <p className="mt-5 border-t border-border pt-5 text-center text-sm text-muted">
          {mode === "register" ? "Уже с нами? " : "Нет аккаунта? "}
          <button
            onClick={() => switchTo(mode === "register" ? "login" : "register")}
            className="font-medium text-accent underline-offset-4 hover:underline"
          >
            {mode === "register" ? "Войти" : "Зарегистрироваться"}
          </button>
        </p>
      </div>
    </Modal>
  );
}
