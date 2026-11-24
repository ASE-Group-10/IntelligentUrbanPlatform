import { createFileRoute } from "@tanstack/react-router";
import { Activity } from "lucide-react";
import { z } from "zod";

import { CityMap } from "@/components/map/city-map";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/features/auth/auth-context";
import {
  CityConditionsLayer,
  CityConditionsPanel,
} from "@/features/city-monitoring/city-conditions";
import { SEGMENT_HIT_LAYER } from "@/features/city-monitoring/use-city-snapshot";

/** `?segment=seg:<osm_way_id>` selects a road, so other screens can link to it. */
const searchSchema = z.object({
  segment: z
    .string()
    .regex(/^seg:[1-9][0-9]*$/)
    .optional()
    .catch(undefined),
});

export const Route = createFileRoute("/")({
  component: CityMapScreen,
  validateSearch: searchSchema,
});

function CityMapScreen() {
  const { segment: selectedKey } = Route.useSearch();
  const navigate = Route.useNavigate();
  // C06 needs a signed-in user; the token comes from the shell session (#96).
  const { accessToken } = useAuth();

  return (
    <div className="relative min-h-[calc(100dvh-3.5rem)] flex-1">
      <h1 className="sr-only">City map</h1>
      <CityMap
        interactiveLayerIds={[SEGMENT_HIT_LAYER]}
        onClick={(event) => {
          const key = event.features?.[0]?.properties?.segmentKey;
          void navigate({
            search: { segment: typeof key === "string" ? key : undefined },
            replace: true,
          });
        }}
      >
        <CityConditionsLayer accessToken={accessToken} />
      </CityMap>
      <Card className="absolute top-3 left-3 w-[min(20rem,calc(100%-4.5rem))] gap-3 bg-card/90 py-4 shadow-lg backdrop-blur">
        <CardHeader className="px-4">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Activity className="size-4 text-primary" />
            City conditions
          </CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <CityConditionsPanel accessToken={accessToken} selectedKey={selectedKey} />
        </CardContent>
      </Card>
    </div>
  );
}
