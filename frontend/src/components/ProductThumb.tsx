import { useState } from "react";
import { Icon, CATEGORY_ICON } from "./Icon";
import type { Category } from "../lib/menu";

// shows the product photo; falls back to the category icon if it's missing or fails to load
export function ProductThumb({
  category,
  name,
  image,
  className = "",
  imageClassName = "",
  iconSize = 64,
  priority = false,
}: {
  category: Category;
  name: string;
  image?: string | null;
  className?: string;
  imageClassName?: string;
  iconSize?: number;
  // the photo of the first screen: fetched at once instead of when it scrolls into view
  priority?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const showImage = Boolean(image) && !failed;

  return (
    <div
      className={`relative flex items-center justify-center overflow-hidden bg-surface-2 ${className}`}
    >
      {showImage ? (
        <img
          // a cached photo may have loaded before react attached the handler
          ref={(img) => {
            if (img?.complete && img.naturalWidth > 0) setLoaded(true);
          }}
          src={image ?? undefined}
          alt={name}
          loading={priority ? "eager" : "lazy"}
          fetchPriority={priority ? "high" : "auto"}
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          data-loaded={loaded || undefined}
          className={`reveal h-full w-full object-cover ${imageClassName}`}
        />
      ) : (
        <>
          <div
            className="absolute inset-0 opacity-70"
            style={{
              background:
                "radial-gradient(120% 120% at 30% 0%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 60%)",
            }}
          />
          <Icon name={CATEGORY_ICON[category]} size={iconSize} strokeWidth={1.1} className="text-muted/40" />
        </>
      )}
    </div>
  );
}
