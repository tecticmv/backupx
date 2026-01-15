import { useState, useMemo } from "react";
import { cn } from "@/lib/utils";

interface ContributionDay {
  date: string;
  success: number;
  failed: number;
  total: number;
}

interface ContributionGraphProps {
  data: ContributionDay[];
  className?: string;
}

export function ContributionGraph({ data, className }: ContributionGraphProps) {
  const [hoveredDay, setHoveredDay] = useState<ContributionDay | null>(null);
  const [tooltipPosition, setTooltipPosition] = useState({ x: 0, y: 0 });

  // Build weeks aligned by actual day of week (Sunday = 0)
  const weeks = useMemo(() => {
    if (!data || data.length === 0) return [];

    const result: (ContributionDay | null)[][] = [];

    // Create a map of date strings to data for quick lookup
    const dataMap = new Map<string, ContributionDay>();
    data.forEach(d => dataMap.set(d.date, d));

    // Get the date range
    const startDate = new Date(data[0].date);
    const endDate = new Date(data[data.length - 1].date);

    // Start from the Sunday of the first week
    const firstSunday = new Date(startDate);
    firstSunday.setDate(startDate.getDate() - startDate.getDay());

    let currentDate = new Date(firstSunday);
    let currentWeek: (ContributionDay | null)[] = [];

    while (currentDate <= endDate) {
      const dateStr = currentDate.toISOString().split('T')[0];
      const dayData = dataMap.get(dateStr) || null;

      currentWeek.push(dayData);

      // If we've completed a week (Saturday), push it and start a new one
      if (currentDate.getDay() === 6) {
        result.push(currentWeek);
        currentWeek = [];
      }

      // Move to next day
      currentDate.setDate(currentDate.getDate() + 1);
    }

    // Push any remaining days in the last week
    if (currentWeek.length > 0) {
      result.push(currentWeek);
    }

    return result;
  }, [data]);

  // Get intensity level (0-4) based on total backups
  const getIntensityLevel = (day: ContributionDay): number => {
    if (day.total === 0) return 0;
    if (day.total === 1) return 1;
    if (day.total <= 3) return 2;
    if (day.total <= 6) return 3;
    return 4;
  };

  // Get color class based on success/failure ratio
  const getColorClass = (day: ContributionDay | null): string => {
    if (!day || day.total === 0) return "bg-muted/40 dark:bg-muted/20";

    const successRatio = day.success / day.total;
    const level = getIntensityLevel(day);

    if (successRatio >= 0.8) {
      // Mostly success - green shades
      const greenLevels = [
        "bg-muted/40 dark:bg-muted/20",
        "bg-emerald-200 dark:bg-emerald-900/60",
        "bg-emerald-300 dark:bg-emerald-800/80",
        "bg-emerald-400 dark:bg-emerald-700",
        "bg-emerald-500 dark:bg-emerald-600",
      ];
      return greenLevels[level];
    } else if (successRatio >= 0.5) {
      // Mixed - amber shades
      const amberLevels = [
        "bg-muted/40 dark:bg-muted/20",
        "bg-amber-200 dark:bg-amber-900/60",
        "bg-amber-300 dark:bg-amber-800/80",
        "bg-amber-400 dark:bg-amber-700",
        "bg-amber-500 dark:bg-amber-600",
      ];
      return amberLevels[level];
    } else {
      // Mostly failed - red shades
      const redLevels = [
        "bg-muted/40 dark:bg-muted/20",
        "bg-red-200 dark:bg-red-900/60",
        "bg-red-300 dark:bg-red-800/80",
        "bg-red-400 dark:bg-red-700",
        "bg-red-500 dark:bg-red-600",
      ];
      return redLevels[level];
    }
  };

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString("en-US", {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  };

  // Get month labels for the graph
  const monthLabels = useMemo(() => {
    const labels: { month: string; weekIndex: number }[] = [];
    let lastMonth = -1;

    weeks.forEach((week, weekIndex) => {
      // Find first non-null day in the week
      const firstDay = week.find(d => d !== null);
      if (firstDay) {
        const date = new Date(firstDay.date);
        const month = date.getMonth();
        if (month !== lastMonth) {
          labels.push({
            month: date.toLocaleDateString("en-US", { month: "short" }),
            weekIndex
          });
          lastMonth = month;
        }
      }
    });

    return labels;
  }, [weeks]);

  const handleMouseEnter = (day: ContributionDay, event: React.MouseEvent) => {
    setHoveredDay(day);
    const rect = event.currentTarget.getBoundingClientRect();
    setTooltipPosition({
      x: rect.left + rect.width / 2,
      y: rect.top - 10,
    });
  };

  if (weeks.length === 0) {
    return (
      <div className={cn("text-sm text-muted-foreground", className)}>
        No backup data available
      </div>
    );
  }

  return (
    <div className={cn("relative", className)}>
      {/* Month labels */}
      <div className="relative h-5 ml-8">
        {monthLabels.map((label, idx) => (
          <span
            key={idx}
            className="absolute text-xs text-muted-foreground"
            style={{ left: `${label.weekIndex * 14}px` }}
          >
            {label.month}
          </span>
        ))}
      </div>

      <div className="flex">
        {/* Day labels */}
        <div className="flex flex-col gap-[3px] pr-2 text-xs text-muted-foreground shrink-0">
          <span className="h-[11px] leading-[11px]"></span>
          <span className="h-[11px] leading-[11px]">Mon</span>
          <span className="h-[11px] leading-[11px]"></span>
          <span className="h-[11px] leading-[11px]">Wed</span>
          <span className="h-[11px] leading-[11px]"></span>
          <span className="h-[11px] leading-[11px]">Fri</span>
          <span className="h-[11px] leading-[11px]"></span>
        </div>

        {/* Grid */}
        <div className="flex gap-[3px] overflow-x-auto pb-1">
          {weeks.map((week, weekIndex) => (
            <div key={weekIndex} className="flex flex-col gap-[3px] shrink-0">
              {Array.from({ length: 7 }).map((_, dayIndex) => {
                const day = week[dayIndex] ?? null;
                return (
                  <div
                    key={dayIndex}
                    className={cn(
                      "w-[11px] h-[11px] rounded-sm shrink-0",
                      day ? "cursor-pointer transition-all hover:ring-1 hover:ring-foreground/50" : "",
                      getColorClass(day)
                    )}
                    onMouseEnter={day ? (e) => handleMouseEnter(day, e) : undefined}
                    onMouseLeave={() => setHoveredDay(null)}
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center justify-end gap-2 mt-3 text-xs text-muted-foreground">
        <span>Less</span>
        <div className="flex gap-[3px]">
          <div className="w-[11px] h-[11px] rounded-sm bg-muted/40 dark:bg-muted/20" />
          <div className="w-[11px] h-[11px] rounded-sm bg-emerald-200 dark:bg-emerald-900/60" />
          <div className="w-[11px] h-[11px] rounded-sm bg-emerald-300 dark:bg-emerald-800/80" />
          <div className="w-[11px] h-[11px] rounded-sm bg-emerald-400 dark:bg-emerald-700" />
          <div className="w-[11px] h-[11px] rounded-sm bg-emerald-500 dark:bg-emerald-600" />
        </div>
        <span>More</span>
      </div>

      {/* Tooltip */}
      {hoveredDay && (
        <div
          className="fixed z-50 px-3 py-2 text-xs bg-popover border rounded-md shadow-md pointer-events-none"
          style={{
            left: tooltipPosition.x,
            top: tooltipPosition.y,
            transform: "translate(-50%, -100%)",
          }}
        >
          <div className="font-medium">{formatDate(hoveredDay.date)}</div>
          {hoveredDay.total === 0 ? (
            <div className="text-muted-foreground">No backups</div>
          ) : (
            <div className="flex gap-3 mt-1">
              <span className="text-emerald-500">{hoveredDay.success} success</span>
              {hoveredDay.failed > 0 && (
                <span className="text-destructive">{hoveredDay.failed} failed</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
