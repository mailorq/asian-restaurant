import { create } from "zustand";

export interface Toast {
  id: number;
  message: string;
  tone: "success" | "error";
}

interface ToastState {
  toasts: Toast[];
  notify: (message: string, tone?: Toast["tone"]) => void;
  dismiss: (id: number) => void;
  clear: () => void;
}

let seq = 0;

export const useToast = create<ToastState>((set) => ({
  toasts: [],
  notify: (message, tone = "success") => {
    const id = ++seq;
    set((s) => ({ toasts: [...s.toasts, { id, message, tone }] }));
    setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), 3200);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  clear: () => set({ toasts: [] }),
}));
