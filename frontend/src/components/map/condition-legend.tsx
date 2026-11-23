const ROAD_STATES = [
  { label: "Open", className: "bg-status-open" },
  { label: "Slow", className: "bg-status-slow" },
  { label: "Closed", className: "bg-status-closed" },
] as const;

/** Key for the road-condition colours used by City State map layers. */
export function ConditionLegend() {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs" aria-label="Road condition key">
      {ROAD_STATES.map(({ label, className }) => (
        <li key={label} className="flex items-center gap-1.5">
          <span className={`h-1 w-5 rounded-full ${className}`} aria-hidden="true" />
          {label}
        </li>
      ))}
      <li className="flex items-center gap-1.5">
        <span className="w-5 border-t-2 border-dashed border-muted-foreground" aria-hidden="true" />
        Uncertain
      </li>
    </ul>
  );
}
