import { useCallback, useEffect, useRef, useState } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';
import { Header } from './header';
import { Sidebar } from './sidebar';
import { BottomNav } from './bottom-nav';
import { KeyboardShortcutsModal } from '../keyboard-shortcuts-modal';
import { useUiStore } from '@/stores/ui-store';

// App-wide chrome: header on top, sidebar on the left (desktop only),
// main content via Outlet, bottom nav on mobile only.
//
// Skip link is the first focusable element so keyboard / screen-reader users
// can jump straight to main content (WCAG 2.4.1).
//
// E.2: global keyboard shortcuts with vim-style two-key chords
// (g→o, g→b, g→s, n→b). Single keys: ?, t, Esc.

export function AppShell() {
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const navigate = useNavigate();
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const pendingChord = useRef<string | null>(null);
  const chordTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      // Skip when user is typing in an input/textarea/select
      const tag = (e.target as HTMLElement).tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if ((e.target as HTMLElement).isContentEditable) return;

      const key = e.key.toLowerCase();

      // Chord resolution: if we have a pending prefix, resolve it
      if (pendingChord.current) {
        const chord = pendingChord.current + key;
        pendingChord.current = null;
        clearTimeout(chordTimer.current);

        switch (chord) {
          case 'go': navigate('/'); return;
          case 'gb': navigate('/bots'); return;
          case 'gs': navigate('/settings'); return;
          case 'nb':
            window.dispatchEvent(new CustomEvent('wizard:open'));
            return;
        }
        return; // unknown chord — swallow silently
      }

      // Chord start: g or n sets a pending prefix
      if (key === 'g' || key === 'n') {
        pendingChord.current = key;
        chordTimer.current = setTimeout(() => {
          pendingChord.current = null;
        }, 500);
        return;
      }

      // Single-key shortcuts
      switch (key) {
        case '?':
          setShortcutsOpen(true);
          break;
        case 't':
          toggleTheme();
          break;
        case 'escape':
          setShortcutsOpen(false);
          break;
      }
    },
    [navigate, toggleTheme]
  );

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  return (
    <div className="flex flex-col min-h-dvh bg-bg-base text-text-primary">
      <a
        href="#main-content"
        className="absolute left-2 top-2 z-50 rounded-md bg-primary px-3 py-2 text-xs font-semibold text-bg-base -translate-y-16 focus-visible:translate-y-0 transition-transform"
      >
        Skip to main content
      </a>
      <Header />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main
          id="main-content"
          tabIndex={-1}
          className="flex-1 min-w-0 overflow-y-auto p-4 md:p-6 pb-20 md:pb-6 focus:outline-none"
        >
          <Outlet />
        </main>
      </div>
      <BottomNav />
      <KeyboardShortcutsModal
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />
    </div>
  );
}
