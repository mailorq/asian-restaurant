import { useEffect, useState } from "react";
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
}: {
  label: string;
  children: React.ReactNode;
  required?: boolean;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-sm font-medium text-muted">
        {label}
        {required && <span className="text-accent"> *</span>}
      </span>
      {children}
    </label>
  );
}

const inputCls =
  "h-11 w-full rounded-xl border border-border bg-surface-2 px-3.5 text-text placeholder:text-muted/70 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20";

// the server lets a phone have a new code a minute after the previous one
const RESEND_SECONDS = 60;

type Mode = "login" | "register" | "recover";

export function AuthModal() {
  const tab = useUI((s) => s.authTab);
  const setTab = (t: "login" | "register") => useUI.setState({ authTab: t });
  const close = useUI((s) => s.closeModal);
  const notify = useToast((s) => s.notify);
  const setUser = useAuth((s) => s.setUser);

  const [recovering, setRecovering] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [phone, setPhone] = useState("+380 ");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [resendIn, setResendIn] = useState(0);
  const [phoneError, setPhoneError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const mode: Mode = tab === "register" ? "register" : recovering ? "recover" : "login";

  useEffect(() => {
    if (resendIn <= 0) return;
    const timer = window.setTimeout(() => setResendIn((s) => s - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [resendIn]);

  function switchTo(next: Mode) {
    setTab(next === "register" ? "register" : "login");
    setRecovering(next === "recover");
    setCodeSent(false);
    setCode("");
    setError(null);
  }

  async function run<T>(request: () => Promise<T>): Promise<T | undefined> {
    setError(null);
    setLoading(true);
    try {
      return await request();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Что-то пошло не так");
      return undefined;
    } finally {
      setLoading(false);
    }
  }

  async function requestCode() {
    if (!isValidUaPhone(phone)) {
      setPhoneError("Введите номер в формате +380 (XX) XXX XX XX");
      return;
    }
    const path = mode === "register" ? "/auth/register/code" : "/auth/password/code";
    const sent = await run(() =>
      api<{ detail: string }>(path, { method: "POST", body: JSON.stringify({ phone }) }),
    );
    if (sent) {
      setCodeSent(true);
      setResendIn(RESEND_SECONDS);
      notify(sent.detail);
    }
  }

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!isValidUaPhone(phone)) {
      setPhoneError("Введите номер в формате +380 (XX) XXX XX XX");
      return;
    }
    if (mode !== "login" && !codeSent) {
      await requestCode();
      return;
    }
    const [path, body, greeting] =
      mode === "login"
        ? ["/auth/login", { phone, password }, "Вход выполнен"]
        : mode === "register"
          ? ["/auth/register", { phone, password, name: name.trim(), code }, "Добро пожаловать!"]
          : ["/auth/password/reset", { phone, password, code }, "Пароль изменен, вход выполнен"];
    const user = await run(() => api<CurrentUser>(path, { method: "POST", body: JSON.stringify(body) }));
    if (user) {
      setUser(user);
      notify(greeting);
      close();
    }
  }

  const title = mode === "login" ? "Вход" : mode === "register" ? "Регистрация" : "Восстановление доступа";
  const action =
    mode === "login"
      ? "Войти"
      : !codeSent
        ? "Получить код"
        : mode === "register"
          ? "Создать аккаунт"
          : "Сменить пароль и войти";

  return (
    <Modal title={title} onClose={close}>
      <div className="mb-5 grid grid-cols-2 gap-1 rounded-full bg-surface-2 p-1">
        {(["login", "register"] as const).map((t) => (
          <button
            key={t}
            onClick={() => switchTo(t)}
            className={`rounded-full py-2 text-sm font-medium transition-colors ${
              tab === t ? "bg-surface text-text shadow-sm" : "text-muted hover:text-text"
            }`}
          >
            {t === "login" ? "Вход" : "Регистрация"}
          </button>
        ))}
      </div>

      <div key={mode} className="anim-fade-up">
        <form onSubmit={submit} className="flex flex-col gap-4">
          {mode === "register" && (
            <Field label="Имя" required>
              <input
                name="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className={inputCls}
                placeholder="Иван"
                autoComplete="name"
                required
                maxLength={150}
              />
            </Field>
          )}

          <Field label="Телефон" required>
            <input
              type="tel"
              inputMode="tel"
              value={phone}
              readOnly={codeSent}
              onChange={(e) => {
                setPhone(formatUaPhone(e.target.value));
                setPhoneError(null);
              }}
              className={`${inputCls} ${phoneError ? "border-danger focus:border-danger focus:ring-danger/20" : ""} ${codeSent ? "opacity-70" : ""}`}
              placeholder="+380 (67) 123 45 67"
              autoComplete="tel"
              required
            />
            {phoneError && <span className="mt-1 block text-xs text-danger">{phoneError}</span>}
            {codeSent && (
              <button
                type="button"
                onClick={() => {
                  setCodeSent(false);
                  setCode("");
                }}
                className="mt-1 text-xs font-medium text-accent hover:underline"
              >
                Изменить номер
              </button>
            )}
          </Field>

          {codeSent && (
            <Field label="Код из SMS" required>
              <input
                name="code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                className={inputCls}
                placeholder="123456"
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                minLength={6}
              />
              <button
                type="button"
                disabled={resendIn > 0 || loading}
                onClick={requestCode}
                className="mt-1 text-xs font-medium text-accent hover:underline disabled:cursor-not-allowed disabled:text-muted disabled:no-underline"
              >
                {resendIn > 0 ? `Отправить код еще раз через ${resendIn} с` : "Отправить код еще раз"}
              </button>
            </Field>
          )}

          {(mode !== "recover" || codeSent) && (
            <Field label={mode === "recover" ? "Новый пароль" : "Пароль"} required>
              <div className="relative">
                <input
                  name="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  type={showPassword ? "text" : "password"}
                  className={`${inputCls} pr-11`}
                  placeholder="••••••••"
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  required
                  minLength={8}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? "Скрыть пароль" : "Показать пароль"}
                  className="absolute right-2 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-lg text-muted transition-colors hover:text-accent"
                >
                  <Icon name={showPassword ? "eyeOff" : "eye"} size={18} />
                </button>
              </div>
            </Field>
          )}

          {error && (
            <p className="rounded-lg bg-danger/10 px-3 py-2 text-sm text-danger">{error}</p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="mt-1 h-11 rounded-xl bg-primary font-medium text-primary-contrast transition-[background-color,transform] duration-200 hover:bg-primary-hover active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loading ? "Подождите…" : action}
          </button>
        </form>

        {mode === "login" && (
          <p className="mt-3 text-center text-sm">
            <button onClick={() => switchTo("recover")} className="text-muted hover:text-accent hover:underline">
              Забыли пароль?
            </button>
          </p>
        )}

        <p className="mt-4 text-center text-sm text-muted">
          {mode === "register" ? "Уже с нами? " : "Нет аккаунта? "}
          <button
            onClick={() => switchTo(mode === "register" ? "login" : "register")}
            className="font-medium text-accent hover:underline"
          >
            {mode === "register" ? "Войти" : "Зарегистрироваться"}
          </button>
        </p>
      </div>
    </Modal>
  );
}
