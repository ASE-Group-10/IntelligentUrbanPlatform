import "@testing-library/jest-dom/vitest";

// jsdom lacks matchMedia, which the theme provider uses to read the system theme.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }),
});

// jsdom does not implement scrolling; the router calls it on navigation.
window.scrollTo = () => {};
