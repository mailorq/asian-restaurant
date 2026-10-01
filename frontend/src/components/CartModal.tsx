import { useEffect, useState } from "react";
import { Modal } from "./Modal";
import { Icon } from "./Icon";
import { ProductThumb } from "./ProductThumb";
import { useUI } from "../stores/ui";
import { useToast } from "../stores/toast";
import { useCartQuery, useConfirmCart, useRemoveItem, useSetItem } from "../api/cart";
import { useCheckout, useLastAddress, useVerifyAddress } from "../api/orders";
import { ApiError } from "../api/client";
import { useAuth } from "../stores/auth";
import { formatPrice } from "../lib/menu";
import { formatUaPhone } from "../lib/phone";

type Step = "cart" | "checkout";

export function CartModal() {
  const { data: cart, isLoading } = useCartQuery();
  const setItem = useSetItem();
  const removeItem = useRemoveItem();
  const checkout = useCheckout();
  const close = useUI((s) => s.closeModal);
  const openModal = useUI((s) => s.openModal);
  const navigate = useUI((s) => s.navigate);
  const notify = useToast((s) => s.notify);
  const user = useAuth((s) => s.user);
  const { data: lastAddress } = useLastAddress(Boolean(user));
  const verify = useVerifyAddress();
  const [step, setStep] = useState<Step>("cart");
  // one key per visit to the form: a resubmit after an answer that never arrived is the same order
  const [checkoutKey, setCheckoutKey] = useState("");
  const [payment, setPayment] = useState<"cash" | "card">("cash");
  const [address, setAddress] = useState("");
  const [recipientName, setRecipientName] = useState("");
  const [addressTouched, setAddressTouched] = useState(false);
  const [nameTouched, setNameTouched] = useState(false);

  const items = cart?.items ?? [];
  const total = cart?.total ?? 0;
  const empty = items.length === 0;
  const confirmCart = useConfirmCart();
  const busy = setItem.isPending || removeItem.isPending;
  const review = cart?.review_order ?? null;

  // prefill recipient from the profile until the user edits it (does not touch the profile)
  useEffect(() => {
    if (user && !nameTouched) setRecipientName(user.name ?? "");
  }, [user, nameTouched]);

  // prefill address from the last used one until the user edits it
  useEffect(() => {
    if (lastAddress?.address && !addressTouched) setAddress(lastAddress.address);
  }, [lastAddress, addressTouched]);

  function checkAddress() {
    const value = address.trim();
    if (value.length < 3 || verify.isPending) return;
    verify.mutate(value, {
      onSuccess: (r) =>
        notify(
          r.verified ? `Адрес найден: ${r.display_name}` : "Адрес не распознан — уточните",
          r.verified ? "success" : "error",
        ),
      onError: () => notify("Проверка адреса временно недоступна", "error"),
    });
  }

  function openCheckout() {
    setCheckoutKey(crypto.randomUUID());
    setStep("checkout");
  }

  function placeOrder(e: React.FormEvent) {
    e.preventDefault();
    if (!user || checkout.isPending) return;
    checkout.mutate(
      {
        address: address.trim(),
        payment_method: payment,
        recipient_name: recipientName.trim(),
        idempotency_key: checkoutKey,
      },
      {
        onSuccess: (order) => {
          notify(`Заказ №${order.id} оформлен`);
          close();
          navigate({ name: "orders" });
        },
        onError: (err) => {
          const body = err instanceof ApiError ? (err.body as { code?: string; message?: string } | null) : null;
          if (err instanceof ApiError && err.status === 409 && body?.code === "checkout_replayed") {
            // this form already placed an order, with the details it was first sent with
            notify(body.message ?? "Заказ уже оформлен", "error");
            close();
            navigate({ name: "orders" });
          } else if (err instanceof ApiError && err.status === 409) {
            notify(body?.message ?? "Корзина изменилась — проверьте состав", "error");
            setStep("cart");
          } else if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
            notify("Войдите, чтобы оформить заказ", "error");
            openModal("auth", "login");
          } else {
            notify(err instanceof Error ? err.message : "Не удалось оформить заказ", "error");
          }
        },
      },
    );
  }

  const title =
    step === "cart" ? "Корзина" : (
      <button onClick={() => setStep("cart")} className="flex items-center gap-2 hover:text-accent">
        <Icon name="arrowLeft" size={20} /> Оформление
      </button>
    );

  return (
    <Modal
      title={title}
      onClose={close}
      footer={
        empty || isLoading ? undefined : step === "cart" ? (
          <div>
            <div className="mb-3 flex items-center justify-between">
              <span className="text-muted">Итого</span>
              <span className="tnum text-xl font-semibold">{formatPrice(total)}</span>
            </div>
            <button
              onClick={openCheckout}
              disabled={review !== null}
              className="h-12 w-full rounded-xl bg-primary font-medium text-primary-contrast transition-[background-color,transform] duration-200 hover:bg-primary-hover active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-60"
            >
              Оформить заказ
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-between">
            <span className="text-muted">К оплате</span>
            <span className="tnum text-xl font-semibold">{formatPrice(total)}</span>
          </div>
        )
      }
    >
      {isLoading ? (
        <ul className="flex flex-col gap-3">
          {[0, 1, 2].map((i) => (
            <li key={i} className="flex items-center gap-3">
              <div className="h-16 w-16 shrink-0 animate-pulse rounded-xl bg-surface-2" />
              <div className="flex-1 space-y-2">
                <div className="h-4 w-2/3 animate-pulse rounded bg-surface-2" />
                <div className="h-3 w-1/4 animate-pulse rounded bg-surface-2" />
              </div>
            </li>
          ))}
        </ul>
      ) : empty ? (
        <div className="flex flex-col items-center py-10 text-center">
          <span className="mb-4 grid h-16 w-16 place-items-center rounded-full bg-surface-2 text-muted">
            <Icon name="cart" size={30} />
          </span>
          <p className="font-medium">Корзина пуста</p>
          <p className="mt-1 text-sm text-muted">Добавьте что-нибудь из меню — мы всё приготовим.</p>
        </div>
      ) : step === "cart" ? (
        <>
          {review !== null && (
            <div className="mb-3 rounded-lg bg-accent/10 px-3 py-2.5 text-sm">
              <p>
                Корзина могла сохранить позиции заказа №{review}, оформленного до обновления сайта. Проверьте
                состав: уже купленное можно удалить.
              </p>
              <button
                onClick={() => confirmCart.mutate()}
                disabled={confirmCart.isPending}
                className="mt-2 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-contrast hover:bg-primary-hover disabled:opacity-60"
              >
                Состав верный
              </button>
            </div>
          )}
          {cart && (cart.removed_items.length > 0 || cart.adjustments.length > 0) && (
            <div className="mb-3 space-y-1.5">
              {cart.removed_items.map((r) => (
                <p
                  key={`r${r.product_id}`}
                  className="rounded-lg bg-danger/10 px-3 py-2 text-xs text-danger"
                >
                  «{r.name}» {r.reason === "out_of_stock" ? "закончился" : "недоступен"} — удалён из
                  корзины
                </p>
              ))}
              {cart.adjustments.map((a) => (
                <p
                  key={`a${a.product_id}`}
                  className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400"
                >
                  Количество «{a.name}» уменьшено до {a.to_qty} шт. из-за остатка
                </p>
              ))}
            </div>
          )}
          <ul className="flex flex-col gap-3">
            {items.map((line) => (
              <li key={line.product_id} className="flex items-center gap-3">
                <ProductThumb
                  category={line.category}
                  name={line.name}
                  image={line.image}
                  className="h-16 w-16 shrink-0 rounded-xl"
                  iconSize={26}
                />
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{line.name}</p>
                  <p className="tnum text-sm text-muted">{formatPrice(line.price)}</p>
                </div>
                <div className="flex items-center gap-1 rounded-full border border-border p-1">
                  <button
                    onClick={() => setItem.mutate({ productId: line.product_id, quantity: line.quantity - 1 })}
                    disabled={busy}
                    aria-label="Меньше"
                    className="grid h-7 w-7 place-items-center rounded-full text-muted hover:bg-surface-2 hover:text-text disabled:opacity-50"
                  >
                    <Icon name="minus" size={15} strokeWidth={2} />
                  </button>
                  <span className="tnum w-6 text-center text-sm font-semibold">{line.quantity}</span>
                  <button
                    onClick={() => setItem.mutate({ productId: line.product_id, quantity: line.quantity + 1 })}
                    disabled={busy}
                    aria-label="Больше"
                    className="grid h-7 w-7 place-items-center rounded-full text-muted hover:bg-surface-2 hover:text-text disabled:opacity-50"
                  >
                    <Icon name="plus" size={15} strokeWidth={2} />
                  </button>
                </div>
                <button
                  onClick={() => removeItem.mutate({ productId: line.product_id })}
                  disabled={busy}
                  aria-label={`Убрать ${line.name}`}
                  className="grid h-8 w-8 place-items-center rounded-full text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                >
                  <Icon name="trash" size={18} />
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : !user ? (
        <div className="flex flex-col items-center py-8 text-center">
          <span className="mb-4 grid h-14 w-14 place-items-center rounded-full bg-surface-2 text-muted">
            <Icon name="user" size={26} />
          </span>
          <p className="font-medium">Войдите, чтобы оформить заказ</p>
          <p className="mt-1 text-sm text-muted">Имя и телефон мы возьмём из вашего аккаунта.</p>
          <button
            onClick={() => openModal("auth", "login")}
            className="mt-4 rounded-full bg-primary px-6 py-2.5 text-sm font-medium text-primary-contrast transition-colors hover:bg-primary-hover"
          >
            Войти
          </button>
        </div>
      ) : (
        <form id="checkout-form" onSubmit={placeOrder} className="flex flex-col gap-4">
          <div className="flex items-center gap-1.5 rounded-xl border border-border bg-surface-2 px-3.5 py-2.5 text-sm text-muted">
            <Icon name="phone" size={14} />
            {user.phone ? formatUaPhone(user.phone) : "Телефон не указан"}
          </div>

          <label className="block">
            <span className="mb-1.5 block text-sm font-medium text-muted">Имя получателя<span className="text-accent"> *</span></span>
            <input
              required
              value={recipientName}
              onChange={(e) => {
                setNameTouched(true);
                setRecipientName(e.target.value);
              }}
              placeholder="Иван"
              className="h-11 w-full rounded-xl border border-border bg-surface-2 px-3.5 focus:border-primary focus:outline-none"
            />
          </label>

          <label className="block">
            <span className="mb-1.5 block text-sm font-medium text-muted">Адрес доставки<span className="text-accent"> *</span></span>
            <div className="flex gap-2">
              <input
                required
                minLength={5}
                value={address}
                onChange={(e) => {
                  setAddressTouched(true);
                  setAddress(e.target.value);
                  if (verify.data) verify.reset();
                }}
                placeholder="ул. Пушкина, 12, кв. 3"
                className="h-11 w-full rounded-xl border border-border bg-surface-2 px-3.5 focus:border-primary focus:outline-none"
              />
              <button
                type="button"
                onClick={checkAddress}
                disabled={verify.isPending || address.trim().length < 3}
                className="shrink-0 rounded-xl border border-border px-3.5 text-sm font-medium text-muted transition-colors hover:border-accent hover:text-accent disabled:opacity-50"
              >
                {verify.isPending ? "…" : "Проверить"}
              </button>
            </div>
            {verify.data && (
              <p
                className={`mt-1.5 flex items-start gap-1.5 text-xs ${
                  verify.data.verified ? "text-emerald-600 dark:text-emerald-400" : "text-danger"
                }`}
              >
                <Icon name={verify.data.verified ? "check" : "pin"} size={13} className="mt-0.5 shrink-0" />
                {verify.data.verified ? verify.data.display_name : "Адрес не распознан — доставим по указанному тексту"}
              </p>
            )}
          </label>

          <fieldset>
            <span className="mb-1.5 block text-sm font-medium text-muted">Оплата</span>
            <div className="grid grid-cols-2 gap-3">
              {([
                { v: "cash", label: "Наличными" },
                { v: "card", label: "Картой" },
              ] as const).map((opt) => (
                <label
                  key={opt.v}
                  className={`cursor-pointer rounded-xl border-2 px-4 py-3 text-center text-sm font-medium transition-colors ${
                    payment === opt.v
                      ? "border-accent bg-accent/10 text-text"
                      : "border-border text-muted hover:border-accent/50"
                  }`}
                >
                  <input
                    type="radio"
                    name="payment"
                    value={opt.v}
                    checked={payment === opt.v}
                    onChange={() => setPayment(opt.v)}
                    className="sr-only"
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </fieldset>

          <button
            type="submit"
            disabled={checkout.isPending}
            className="mt-1 h-12 rounded-xl bg-primary font-medium text-primary-contrast transition-[background-color,transform] duration-200 hover:bg-primary-hover active:scale-[0.99] disabled:opacity-60"
          >
            {checkout.isPending ? "Оформляем…" : "Подтвердить заказ"}
          </button>
        </form>
      )}
    </Modal>
  );
}
