import en from "../locales/en.json";
import ru from "../locales/ru.json";

const LOCALES = { ru, en } as const;
export type Locale = keyof typeof LOCALES;

// active ui locale: russian for now, english prepared in ../locales/en.json.
// switch here (or wire to a store) to change the interface language.
export const locale: Locale = "ru";
export const strings = LOCALES[locale];
