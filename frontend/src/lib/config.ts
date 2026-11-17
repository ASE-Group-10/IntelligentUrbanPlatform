/** Browser-side configuration. Only VITE_* variables are exposed by Vite. */
export const config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? "/api",
  mapStyleUrl:
    import.meta.env.VITE_MAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/positron",
  mapStyleUrlDark:
    import.meta.env.VITE_MAP_STYLE_URL_DARK ?? "https://tiles.openfreemap.org/styles/dark",
} as const;

/** Dublin city centre, the demo scenario's home. */
export const DUBLIN = { longitude: -6.2603, latitude: 53.3498, zoom: 12.5 } as const;
