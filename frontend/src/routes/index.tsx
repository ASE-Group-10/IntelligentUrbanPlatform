import { createFileRoute } from "@tanstack/react-router";
import { Activity } from "lucide-react";

import { CityMap } from "@/components/map/city-map";
import { ConditionLegend } from "@/components/map/condition-legend";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export const Route = createFileRoute("/")({
  component: CityMapScreen,
});

function CityMapScreen() {
  return (
    <div className="relative min-h-[calc(100dvh-3.5rem)] flex-1">
      <h1 className="sr-only">City map</h1>
      <CityMap />
      <Card className="absolute top-3 left-3 w-[min(20rem,calc(100%-4.5rem))] gap-3 bg-card/90 py-4 shadow-lg backdrop-blur">
        <CardHeader className="px-4">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Activity className="size-4 text-primary" />
            City conditions
          </CardTitle>
          <CardDescription className="text-xs">
            Road closures and slowdowns from C06 City State will appear on this map.
          </CardDescription>
        </CardHeader>
        <CardContent className="px-4">
          <ConditionLegend />
        </CardContent>
      </Card>
    </div>
  );
}
