import { useTheme } from "@/lib/theme";
import type { ReactNode } from "react";
import MapGL, { AttributionControl, NavigationControl, ScaleControl } from "react-map-gl/maplibre";

import { config, DUBLIN } from "@/lib/config";

interface CityMapProps {
  /** Map layers and markers, e.g. City State segments or plan routes. */
  children?: ReactNode;
}

/**
 * Shared Dublin base map. Uses OpenFreeMap vector tiles (no API key) with a
 * muted style so coloured overlays such as closures stand out.
 */
export function CityMap({ children }: CityMapProps) {
  const { resolvedTheme } = useTheme();
  const mapStyle = resolvedTheme === "dark" ? config.mapStyleUrlDark : config.mapStyleUrl;

  return (
    <MapGL
      initialViewState={DUBLIN}
      mapStyle={mapStyle}
      style={{ position: "absolute", inset: 0 }}
      attributionControl={false}
      minZoom={10}
      maxZoom={18}
    >
      <NavigationControl position="top-right" showCompass={false} />
      <ScaleControl position="bottom-left" unit="metric" />
      <AttributionControl position="bottom-right" compact />
      {children}
    </MapGL>
  );
}
