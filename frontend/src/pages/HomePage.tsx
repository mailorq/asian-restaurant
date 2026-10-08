import { ProductCard } from "../components/ProductCard";
import { ProductThumb } from "../components/ProductThumb";
import { Chopsticks } from "../components/Chopsticks";
import { Icon } from "../components/Icon";
import { useUI } from "../stores/ui";
import { useProducts } from "../api/menu";
import { CATEGORY_LABELS, type Category } from "../lib/menu";
import { plural } from "../lib/format";

const FACTS = [
  { icon: "star", text: "Свежие продукты каждый день" },
  { icon: "clock", text: "Горячим за 60 минут" },
  { icon: "cart", text: "Картой или наличными" },
];

const CATEGORIES: Category[] = ["dish", "drink", "dessert"];

export function HomePage() {
  const navigate = useUI((s) => s.navigate);
  const { data } = useProducts();
  const products = data ?? [];
  const featured = products.filter((p) => p.is_featured);
  // the plate holds the first featured dish; a drink or a dessert reads worse from above
  const signature = featured.find((p) => p.category === "dish" && p.image) ?? featured.find((p) => p.image);

  return (
    <div>
      {/* the chopsticks lean past the plate; clipped here so a phone never scrolls sideways */}
      <section className="overflow-x-clip border-b border-border">
        <div className="mx-auto grid max-w-6xl items-center gap-12 px-4 pb-14 pt-10 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:gap-16 lg:py-20">
          <div className="anim-hero order-2 lg:order-1">
            <h1 className="max-w-xl text-balance text-[2.75rem] font-bold leading-[1.02] tracking-tight sm:text-6xl lg:text-7xl">
              Вкус Азии у вас дома
            </h1>
            <p className="mt-6 max-w-md text-lg leading-relaxed text-muted">
              Рамен, суши, вок и десерты от шефа. Собрали лучшее из Японии, Кореи, Таиланда и Китая в одном меню.
            </p>
            <div className="mt-9 flex flex-wrap items-center gap-3">
              <button onClick={() => navigate({ name: "menu" })} className="btn btn-primary h-12 px-7 text-base">
                Смотреть меню
              </button>
              <a href="#popular" className="btn btn-ghost h-12 px-5 text-base">
                Популярное
              </a>
            </div>
            <ul className="mt-12 flex flex-col gap-3 border-t border-border pt-6 text-sm text-muted">
              {FACTS.map((fact) => (
                <li key={fact.text} className="flex items-center gap-2.5 whitespace-nowrap">
                  <Icon name={fact.icon} size={17} className="shrink-0 text-accent" />
                  <span>{fact.text}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="relative order-1 mx-auto w-full max-w-[22rem] sm:max-w-md lg:order-2 lg:max-w-none">
            {/* the plate: the photo cropped round, a hairline rim around it */}
            <div className="relative aspect-square rounded-full border border-accent/30 p-3 sm:p-4">
              <div className="anim-scale-in h-full w-full overflow-hidden rounded-full shadow-lg">
                {signature ? (
                  <ProductThumb
                    category={signature.category}
                    name={signature.name}
                    image={signature.image}
                    priority
                    className="h-full w-full"
                    imageClassName="scale-[1.18]"
                  />
                ) : (
                  <div className="h-full w-full bg-surface-2" />
                )}
              </div>
            </div>
            <Chopsticks className="anim-chopsticks pointer-events-none absolute bottom-[6%] right-[-4%] w-[62%] origin-[85%_50%] -rotate-[32deg]" />
          </div>
        </div>
      </section>

      <section id="popular" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-16 sm:px-6 sm:py-20">
        <div className="mb-10 flex items-end justify-between gap-6">
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">Популярное</h2>
          <button
            onClick={() => navigate({ name: "menu" })}
            className="text-sm font-medium text-muted underline-offset-4 transition-colors hover:text-text hover:underline"
          >
            Открыть меню
          </button>
        </div>
        <div className="grid grid-cols-1 gap-x-6 gap-y-12 sm:grid-cols-2 lg:grid-cols-3">
          {featured.map((product) => (
            <ProductCard key={product.id} product={product} />
          ))}
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-4 pb-8 sm:px-6">
        <h2 className="mb-8 text-3xl font-bold tracking-tight sm:text-4xl">Разделы меню</h2>
        <div className="grid gap-4 sm:grid-cols-3">
          {CATEGORIES.map((category) => {
            const inCategory = products.filter((p) => p.category === category);
            const cover = inCategory.find((p) => p.image);
            return (
              <button
                key={category}
                onClick={() => navigate({ name: "menu" })}
                className="group relative aspect-[16/11] overflow-hidden rounded-2xl text-left shadow-sm"
              >
                {cover && (
                  <ProductThumb
                    category={category}
                    name={cover.name}
                    image={cover.image}
                    className="absolute inset-0 h-full w-full"
                    imageClassName="transition-transform duration-[var(--dur-slow)] ease-[var(--ease-out)] group-hover:scale-[1.04]"
                  />
                )}
                {/* the label sits on a dark scrim in both themes, so it stays legible over any photo */}
                <span className="absolute inset-0 bg-gradient-to-t from-black/75 via-black/20 to-transparent" />
                <span className="absolute inset-x-0 bottom-0 flex items-end justify-between gap-3 p-5 text-white">
                  <span className="font-display text-2xl font-semibold">{CATEGORY_LABELS[category]}</span>
                  <span className="tnum text-sm text-white/75">
                    {inCategory.length} {plural(inCategory.length, ["позиция", "позиции", "позиций"])}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
}
