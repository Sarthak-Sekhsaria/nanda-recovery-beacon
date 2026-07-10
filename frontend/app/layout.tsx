import type { Metadata } from "next";
import Link from "next/link";

import { Nav } from "@/components/nav";
import { PUBLIC_API_URL } from "@/lib/api";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "NANDA Recovery Beacon",
    template: "%s · Recovery Beacon",
  },
  description:
    "Recovery control center for interrupted AI-agent workflows: checkpoints, leases, and audited handovers.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-base-800 focus:px-3 focus:py-2 focus:text-sm"
        >
          Skip to content
        </a>

        <header className="sticky top-0 z-40 border-b border-base-800 bg-base-950/85 backdrop-blur">
          <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-6 gap-y-3 px-6 py-3">
            <Link href="/" className="flex items-center gap-2.5">
              <span
                className="relative flex h-2.5 w-2.5 items-center justify-center"
                aria-hidden
              >
                <span className="absolute h-2.5 w-2.5 animate-ping rounded-full bg-orange-500/50" />
                <span className="h-2 w-2 rounded-full bg-orange-400" />
              </span>
              <span className="text-sm font-semibold tracking-tight text-ink-100">
                NANDA Recovery Beacon
              </span>
            </Link>

            <Nav />

            <div className="ml-auto flex items-center gap-3">
              <a href={`${PUBLIC_API_URL}/skill.md`} className="text-xs text-ink-400 hover:text-ink-200">
                /skill.md
              </a>
              <a href={`${PUBLIC_API_URL}/docs`} className="text-xs text-ink-400 hover:text-ink-200">
                OpenAPI
              </a>
            </div>
          </div>
        </header>

        <main id="main" className="mx-auto max-w-[1400px] px-6 py-8">
          {children}
        </main>

        <footer className="mx-auto max-w-[1400px] px-6 pb-10 pt-4">
          <p className="border-t border-base-800 pt-4 text-xs text-ink-400">
            Recovery infrastructure for agent networks. The API is the product; this dashboard is a
            window onto it. All data on this page comes from the live API.
          </p>
        </footer>
      </body>
    </html>
  );
}
