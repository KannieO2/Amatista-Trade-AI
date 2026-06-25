// Test setup — runs before every test file. Imports jest-dom's matchers
// (.toBeInTheDocument, .toHaveClass, .toHaveTextContent, etc.) so they
// extend vitest's expect.
import '@testing-library/jest-dom/vitest';

// Recharts uses ResizeObserver internally; jsdom doesn't ship one.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
