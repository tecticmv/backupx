import { useState } from "react";
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

  // Group data by weeks (7 days per column)
  const weeks: ContributionDay[][] = [];
  for (let i = 0; i < data.length; i += 7) {
    weeks.push(data.slice(i, i + 7));
  }

  // Get intensity level (0-4) based on total backups
  const getIntensityLevel = (day: ContributionDay): number => {
    if (day.total === 0) return 0;
    if (day.total === 1) return 1;
    if (day.total <= 3) return 2;
    if (day.total <= 6) return 3;
    return 4;
  };

  // Get color class based on success/failure ratio
  const getColorClass = (day: ContributionDay): string => {
    if (day.total === 0) return "bg-muted/50";

    const successRatio = day.success / day.total;
    const level = getIntensityLevel(day);

    if (successRatio >= 0.8) {
      // Mostly success - green shades
      const greenLevels = [
        "bg-muted/50",
        "bg-emerald-200 dark:bg-emerald-900/50",
        "bg-emerald-300 dark:bg-emerald-800/70",
        "bg-emerald-400 dark:bg-emerald-700",
        "bg-emerald-500 dark:bg-emerald-600",
      ];
      return greenLevels[level];
    } else if (successRatio >= 0.5) {
      // Mixed - amber shades
      const amberLevels = [
        "bg-muted/50",
        "bg-amber-200 dark:bg-amber-900/50",
        "bg-amber-300 dark:bg-amber-800/70",
        "bg-amber-400 dark:bg-amber-700",
        "bg-amber-500 dark:bg-amber-600",
      ];
      return amberLevels[level];
    } else {
      // Mostly failed - red shades
      const redLevels = [
        "bg-muted/50",
        "bg-red-200 dark:bg-red-900/50",
        "bg-red-300 dark:bg-red-800/70",
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
  const getMonthLabels = () => {
    const labels: { month: string; index: number }[] = [];
    let lastMonth = "";

    weeks.forEach((week, weekIndex) => {
      if (week.length > 0) {
        const date = new Date(week[0].date);
        const month = date.toLocaleDateString("en-US", { month: "short" });
        if (month !== lastMonth) {
          labels.push({ month, index: weekIndex });
          lastMonth = month;
        }
      }
    });

    return labels;
  };

  const monthLabels = getMonthLabels();

  const handleMouseEnter = (day: ContributionDay, event: React.MouseEvent) => {
    setHoveredDay(day);
    const rect = event.currentTarget.getBoundingClientRect();
    setTooltipPosition({
      x: rect.left + rect.width / 2,
      y: rect.top - 10,
    });
  };

  return (
    <div className={cn("relative", className)}>
      {/* Month labels */}
      <div className="flex mb-1 ml-8">
        {monthLabels.map((label, idx) => (
          <div
            key={idx}
            className="text-xs text-muted-foreground"
            style={{
              marginLeft: idx === 0 ? label.index * 12 : (label.index - (monthLabels[idx - 1]?.index || 0)) * 12 - 24,
              minWidth: "24px",
            }}
          >
            {label.month}
          </div>
        ))}
      </div>

      <div className="flex">
        {/* Day labels */}
        <div className="flex flex-col justify-between pr-2 text-xs text-muted-foreground h-[84px]">
          <span className="h-3"></span>
          <span className="h-3">Mon</span>
          <span className="h-3"></span>
          <span className="h-3">Wed</span>
          <span className="h-3"></span>
          <span className="h-3">Fri</span>
          <span className="h-3"></span>
        </div>

        {/* Grid */}
        <div className="flex gap-[2px] overflow-x-auto">
          {weeks.map((week, weekIndex) => (
            <div key={weekIndex} className="flex flex-col gap-[2px]">
              {week.map((day, dayIndex) => (
                <div
                  key={dayIndex}
                  className={cn(
                    "w-[10px] h-[10px] rounded-sm cursor-pointer transition-all hover:ring-1 hover:ring-foreground/30",
                    getColorClass(day)
                  )}
                  onMouseEnter={(e) => handleMouseEnter(day, e)}
                  onMouseLeave={() => setHoveredDay(null)}
                />
              ))}
              {/* Fill empty cells at end of last week */}
              {week.length < 7 &&
                Array(7 - week.length)
                  .fill(null)
                  .map((_, i) => (
                    <div
                      key={`empty-${i}`}
                      className="w-[10px] h-[10px] rounded-sm"
                    />
                  ))}
            </div>
          ))}
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center justify-end gap-2 mt-2 text-xs text-muted-foreground">
        <span>Less</span>
        <div className="flex gap-[2px]">
          <div className="w-[10px] h-[10px] rounded-sm bg-muted/50" />
          <div className="w-[10px] h-[10px] rounded-sm bg-emerald-200 dark:bg-emerald-900/50" />
          <div className="w-[10px] h-[10px] rounded-sm bg-emerald-300 dark:bg-emerald-800/70" />
          <div className="w-[10px] h-[10px] rounded-sm bg-emerald-400 dark:bg-emerald-700" />
          <div className="w-[10px] h-[10px] rounded-sm bg-emerald-500 dark:bg-emerald-600" />
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
