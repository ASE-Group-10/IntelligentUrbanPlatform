import { Construction } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

import { PageHeader } from "./page-header";

interface ScreenPlaceholderProps {
  title: string;
  description: string;
  component: string;
  owners: string;
  planned: string[];
}

/** Stand-in for a screen the owning pair has not built yet. */
export function ScreenPlaceholder({
  title,
  description,
  component,
  owners,
  planned,
}: ScreenPlaceholderProps) {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-4 sm:p-6">
      <PageHeader title={title} description={description} />
      <Card className="border-dashed">
        <CardHeader>
          <div className="flex items-center gap-2 text-muted-foreground">
            <Construction className="size-4" />
            <span className="font-mono text-xs">{component}</span>
          </div>
          <CardTitle>Not built yet</CardTitle>
          <CardDescription>
            Owned by {owners}. Build it in <code className="text-xs">src/features/</code> and render
            it from this route.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="grid gap-2 text-sm sm:grid-cols-2">
            {planned.map((item) => (
              <li key={item} className="flex gap-2">
                <span
                  className="mt-2 size-1.5 shrink-0 rounded-full bg-primary"
                  aria-hidden="true"
                />
                {item}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
