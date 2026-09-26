"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { ThemeProvider } from "@/components/shell/theme";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import type { Theme } from "@/lib/theme";

export function Providers({ theme, children }: { theme: Theme; children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            refetchOnWindowFocus: false,
            // Retry only transient failures; 4xx answers are final.
            retry: (count, err) =>
              count < 2 && (!(err instanceof ApiError) || err.status === 0 || err.status >= 500),
          },
        },
      }),
  );
  return (
    <ThemeProvider initial={theme}>
      <QueryClientProvider client={client}>
        <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
