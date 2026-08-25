"use client";

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/skeleton";

const CalendarView = dynamic(() => import("@/components/tasks/calendar-view"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center p-4">
      <Skeleton className="h-full w-full rounded-xl" />
    </div>
  ),
});

export default function CalendarPage() {
  return (
    <div className="h-full">
      <CalendarView />
    </div>
  );
}
