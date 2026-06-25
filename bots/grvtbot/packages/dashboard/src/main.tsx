import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/globals.css';
import App from './App';
import { applyThemeToDocument, useUiStore } from './stores/ui-store';

// Apply persisted theme BEFORE React mounts so we don't flash the wrong
// background. The store rehydrates synchronously from localStorage.
applyThemeToDocument(useUiStore.getState().theme);

// Stale-chunk recovery. After a deploy, the index.html still cached in the
// user's browser may reference asset hashes that no longer exist on the
// server. The first React.lazy() import for a missing chunk fires a
// `vite:preloadError` event — we catch it once and hard-reload so the
// browser fetches the new manifest. Without this, the user sees a render
// error and has to know to Ctrl+Shift+R themselves.
let reloadingForStaleChunk = false;
window.addEventListener('vite:preloadError', (event) => {
  if (reloadingForStaleChunk) return;
  reloadingForStaleChunk = true;
  // Prevent React's error boundary from catching this — we're reloading.
  event.preventDefault();
  console.warn('[stale-chunk] preload failed, forcing reload', event);
  window.location.reload();
});

const root = document.getElementById('root');
if (!root) throw new Error('#root element not found');

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>
);
