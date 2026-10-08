import { Icon } from "./Icon";
import { useUI } from "../stores/ui";

export function ThemeToggle() {
  const theme = useUI((s) => s.theme);
  const toggle = useUI((s) => s.toggleTheme);

  return (
    <button
      onClick={toggle}
      aria-label={theme === "dark" ? "Светлая тема" : "Темная тема"}
      className="icon-btn"
    >
      <Icon name={theme === "dark" ? "sun" : "moon"} size={20} />
    </button>
  );
}
