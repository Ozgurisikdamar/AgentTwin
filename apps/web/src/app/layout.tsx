import type { Metadata } from "next";
import localFont from "next/font/local";
import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "./providers";

// Inter and JetBrains Mono (both SIL OFL 1.1), self-hosted from pinned npm
// packages: `next build` needs no network access and the font files are covered
// by the lockfile. Inter is the complete variable font, not a Latin subset, so
// the UI's arrows, Δ and κ render in the same face. next/font uses each const's
// name as the CSS font-family name.
const inter = localFont({
  src: "../../node_modules/inter-ui/variable/InterVariable.woff2",
  weight: "100 900",
  style: "normal",
  display: "swap",
  variable: "--font-inter",
});

const jetbrainsMono = localFont({
  src: "../../node_modules/@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2",
  weight: "100 800",
  style: "normal",
  display: "swap",
  variable: "--font-jetbrains-mono",
  // The generated fallback is metric-matched to Arial, which suits Inter only.
  adjustFontFallback: false,
});

export const metadata: Metadata = {
  title: { default: "AgentTwin", template: "%s · AgentTwin" },
  description: "Production assurance for AI agents: traces, simulation, regression mining and release gates.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`}>
      <body className="min-h-full font-sans">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
