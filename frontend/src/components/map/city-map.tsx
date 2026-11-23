import { useTheme } from "@/lib/theme";
import type { ReactNode } from "react";
import { useState } from "react";
import MapGL, {
  AttributionControl,
  NavigationControl,
  ScaleControl,
  type MapLayerMouseEvent,
} from "react-map-gl/maplibre";

import { config, DUBLIN } from "@/lib/config";

interface CityMapProps {
  /** Map layers and markers, e.g. City State segments or plan routes. */
  children?: ReactNode;
  /** Layers whose features take clicks; the cursor turns into a pointer over them. */
  interactiveLayerIds?: string[];
  onClick?: (event: MapLayerMouseEvent) => void;
}

/**
 * Shared Dublin base map. Uses OpenFreeMap vector tiles (no API key) with a
 * muted style so coloured overlays such as closures stand out.
 */
export function CityMap({ children, interactiveLayerIds, onClick }: CityMapProps) {
  const { resolvedTheme } = useTheme();
  const [hovering, setHovering] = useState(false);
  const mapStyle = resolvedTheme === "dark" ? config.mapStyleUrlDark : config.mapStyleUrl;

  return (
    <MapGL
      initialViewState={DUBLIN}
      mapStyle={mapStyle}
      style={{ position: "absolute", inset: 0 }}
      attributionControl={false}
      minZoom={10}
      maxZoom={18}
      interactiveLayerIds={interactiveLayerIds}
      onClick={onClick}
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      cursor={hovering ? "pointer" : undefined}
    >
      <NavigationControl position="top-right" showCompass={false} />
      <ScaleControl position="bottom-left" unit="metric" />
      <AttributionControl position="bottom-right" compact />
      {children}
    </MapGL>
  );
}
