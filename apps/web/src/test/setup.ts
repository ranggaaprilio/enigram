import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// jsdom has no matchMedia: the theme and the sidebar's phone check read it; tests run as a
// desktop in light mode.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }),
})

// Nor ResizeObserver, which Radix menus measure with.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// Nor scrollIntoView, which cmdk calls on the active item of the command palette.
Element.prototype.scrollIntoView ??= function () {}

afterEach(() => {
  localStorage.clear()
  cleanup()
  vi.unstubAllGlobals()
})
