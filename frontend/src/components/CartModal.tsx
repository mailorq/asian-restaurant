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
          r.verified ? `Адрес найден: ${r.display_name}` : "Адрес не распознан, уточните его",
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
            notify(body?.message ?? "Корзина изменилась, проверьте состав", "error");
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
      <button
        onClick={() => setStep("cart")}
        aria-label="Назад к корзине"
        className="-ml-1 flex items-center gap-2 rounded-lg transition-colors hover:text-accent"
      >
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
            <div className="mb-4 flex items-baseline justify-between">
              <span className="text-muted">Итого</span>
              <span className="tnum text-2xl font-semibold">{formatPrice(total)}</span>
            </div>
            <button onClick={openCheckout} disabled={review !== null} className="btn btn-primary h-12 w-full text-base">
              Оформить заказ
            </button>
            {review !== null && (
              <p className="mt-2 text-center text-xs text-muted">Сначала подтвердите состав корзины</p>
            )}
          </div>
        ) : (
          <div className="flex items-baseline justify-between">
            <span className="text-muted">К оплате</span>
            <span className="tnum text-2xl font-semibold">{formatPrice(total)}</span>
          </div>
        )
      }
    >
      {isLoading ? (
        <ul className="flex flex-col gap-4">
          {[0, 1, 2].map((i) => (
            <li key={i} className="flex items-center gap-4">
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
          <p className="mt-1 max-w-xs text-sm text-muted">Выберите блюда в меню, и они появятся здесь.</p>
          <button
            onClick={() => {
              close();
              navigate({ name: "menu" });
            }}
            className="btn btn-secondary mt-5 h-11"
          >
            Открыть меню
          </button>
        </div>
      ) : step === "cart" ? (
        <>
          {review !== null && (
            <div className="mb-5 rounded-2xl border border-accent/35 bg-accent/10 px-4 py-3.5 text-sm">
              <p className="leading-relaxed">
                Корзина могла сохранить позиции заказа №{review}, оформленного до обновления сайта. Проверьте
                состав: уже купленное можно удалить.
              </p>
              <button
                onClick={() => confirmCart.mutate()}
                disabled={confirmCart.isPending}
                className="btn btn-primary mt-3 h-10 min-h-10 px-4 text-sm"
              >
                Состав верный
              </button>
            </div>
          )}
          {cart && (cart.removed_items.length > 0 || cart.adjustments.length > 0) && (
            <div className="mb-4 space-y-2">
              {cart.removed_items.map((r) => (
                <p key={`r${r.product_id}`} className="rounded-xl bg-danger/10 px-3.5 py-2.5 text-sm text-danger">
                  «{r.name}» {r.reason === "out_of_stock" ? "закончился" : "недоступен"} и убран из корзины
                </p>
              ))}
              {cart.adjustments.map((a) => (
                <p
                  key={`a${a.product_id}`}
                  className="rounded-xl bg-amber-500/10 px-3.5 py-2.5 text-sm text-amber-700 dark:text-amber-400"
                >
                  Количество «{a.name}» уменьшено до {a.to_qty} шт. по остатку
                </p>
              ))}
            </div>
          )}
          <ul className="divide-y divide-border">
            {items.map((line) => (
              <li key={line.product_id} className="flex items-center gap-4 py-3.5 first:pt-0 last:pb-0">
                <ProductThumb
                  category={line.category}
                  name={line.name}
                  image={line.image}
                  className="h-16 w-16 shrink-0 rounded-xl"
                  iconSize={26}
                />
                <div className="min-w-0 flex-1">
                  <p className="line-clamp-2 font-medium leading-snug">{line.name}</p>
                  <p className="tnum mt-0.5 text-sm text-muted">
                    {line.quantity > 1 ? `${line.quantity} × ${formatPrice(line.price)}` : formatPrice(line.price)}
                  </p>
                </div>
                <div className="flex flex-col items-end gap-2">
                  <span className="tnum text-sm font-semibold">{formatPrice(line.price * line.quantity)}</span>
                  <div className="flex items-center gap-1">
                    <div className="flex items-center rounded-full border border-border">
                      <button
                        onClick={() => setItem.mutate({ productId: line.product_id, quantity: line.quantity - 1 })}
                        disabled={busy}
                        aria-label={`Меньше: ${line.name}`}
                        className="icon-btn h-8 w-8"
                      >
                        <Icon name="minus" size={15} strokeWidth={2} />
                      </button>
                      <span className="tnum w-6 text-center text-sm font-semibold">{line.quantity}</span>
                      <button
                        onClick={() => setItem.mutate({ productId: line.product_id, quantity: line.quantity + 1 })}
                        disabled={busy}
                        aria-label={`Больше: ${line.name}`}
                        className="icon-btn h-8 w-8"
                      >
                        <Icon name="plus" size={15} strokeWidth={2} />
                      </button>
                    </div>
                    <button
                      onClick={() => removeItem.mutate({ productId: line.product_id })}
                      disabled={busy}
                      aria-label={`Убрать ${line.name}`}
                      className="icon-btn h-8 w-8 hover:bg-danger/10 hover:text-danger"
                    >
                      <Icon name="trash" size={16} />
                    </button>
                  </div>
                </div>
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
          <p className="mt-1 text-sm text-muted">Имя и телефон возьмем из вашего аккаунта.</p>
          <button onClick={() => openModal("auth", "login")} className="btn btn-primary mt-5 h-11 px-6">
            Войти
          </button>
        </div>
      ) : (
        <form id="checkout-form" onSubmit={placeOrder} className="flex flex-col gap-5">
          <div className="flex items-center gap-2 rounded-xl bg-surface-2 px-4 py-3 text-sm text-muted">
            <Icon name="phone" size={15} />
            <span className="tnum">{user.phone ? formatUaPhone(user.phone) : "Телефон не указан"}</span>
          </div>

          <label className="block">
            <span className="field-label">
              Имя получателя<span className="text-accent"> *</span>
            </span>
            <input
              required
              value={recipientName}
              onChange={(e) => {
                setNameTouched(true);
                setRecipientName(e.target.value);
              }}
              placeholder="Иван"
              autoComplete="name"
              className="input"
            />
          </label>

          <label className="block">
            <span className="field-label">
              Адрес доставки<span className="text-accent"> *</span>
            </span>
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
                autoComplete="street-address"
                className="input"
              />
              <button
                type="button"
                onClick={checkAddress}
                disabled={verify.isPending || address.trim().length < 3}
                className="btn btn-secondary h-12 shrink-0 px-4 text-sm"
              >
                {verify.isPending ? <Spinner /> : "Проверить"}
              </button>
            </div>
            {verify.data && (
              <p
                className={`mt-2 flex items-start gap-1.5 text-sm ${
                  verify.data.verified ? "text-emerald-700 dark:text-emerald-400" : "text-danger"
                }`}
              >
                <Icon name={verify.data.verified ? "check" : "pin"} size={15} className="mt-0.5 shrink-0" />
                {verify.data.verified ? verify.data.display_name : "Адрес не распознан, доставим по указанному тексту"}
              </p>
            )}
          </label>

          <fieldset>
            <legend className="field-label">Оплата</legend>
            <div className="grid grid-cols-2 gap-3">
              {([
                { v: "cash", label: "Наличными" },
                { v: "card", label: "Картой" },
              ] as const).map((opt) => (
                <label
                  key={opt.v}
                  className={`flex h-12 cursor-pointer items-center justify-center rounded-xl border text-sm font-medium transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-accent ${
                    payment === opt.v
                      ? "border-accent bg-accent/10 text-text"
                      : "border-border text-muted hover:border-text/30 hover:text-text"
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

          <button type="submit" disabled={checkout.isPending} className="btn btn-primary mt-1 h-12 w-full text-base">
            {checkout.isPending && <Spinner />}
            {checkout.isPending ? "Оформляем заказ" : "Подтвердить заказ"}
          </button>
        </form>
      )}
    </Modal>
  );
}

function Spinner() {
  return <span aria-hidden className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}
