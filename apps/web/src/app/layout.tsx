import type { Metadata } from "next";
import { cookies } from "next/headers";
import type { ReactNode } from "react";
import { THEME_COOKIE, parseTheme } from "@/lib/theme";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: { default: "AgentTwin", template: "%s · AgentTwin" },
  description: "Production assurance for AI agents: traces, simulation, regression mining and release gates.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Rendered in the chosen theme: no script decides it before the first paint.
  const theme = parseTheme((await cookies()).get(THEME_COOKIE)?.value);
  return (
    <html lang="en" data-theme={theme}>
      <body className="min-h-full font-sans">
        <Providers theme={theme}>{children}</Providers>
      </body>
    </html>
  );
}
