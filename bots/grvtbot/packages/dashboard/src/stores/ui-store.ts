// UI-only state. Anything that touches server data lives in TanStack Query.
// Theme is persisted to localStorage; other flags are session-scoped.

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type Theme = 'dark' | 'light';

interface UiState {
  sidebarCollapsed: boolean;
  theme: Theme;
  toggleSidebar: () => void;
  setSidebar: (collapsed: boolean) => void;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      theme: 'dark',
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebar: (collapsed) => set({ sidebarCollapsed: collapsed }),
      setTheme: (theme) => set({ theme }),
      toggleTheme: () => set((s) => ({ theme: s.theme === 'dark' ? 'light' : 'dark' })),
    }),
    {
      name: 'grvt-grid-ui',
      // Only persist the theme — sidebar state is per-session.
      partialize: (state) => ({ theme: state.theme }),
    }
  )
);

/**
 * Sync the current theme to the <html> element. Called from main.tsx so the
 * paint happens before React mounts (avoiding a flash of the wrong theme).
 */
export function applyThemeToDocument(theme: Theme): void {
  const html = document.documentElement;
  html.dataset.theme = theme;
  html.classList.toggle('light', theme === 'light');
  html.classList.toggle('dark', theme === 'dark');
}
