import type { Metadata } from "next";
import { SessionProvider } from "next-auth/react";

/**
 * Fonts are self-hosted through @fontsource rather than `next/font/google`.
 *
 * `next/font/google` downloads the woff2 files from fonts.gstatic.com during
 * `next build`. That makes the build depend on outbound network access at image
 * build time, and it broke the Docker build: the font requests returned 404 in
 * the builder, after which Turbopack's fallback collapsed into a cascade of
 * "Can't resolve '@vercel/turbopack-next/internal/font/google/font'". It passed
 * locally only because the fetch succeeded there and Next cached the result —
 * which is exactly the kind of works-on-my-machine dependency a container build
 * should not have.
 *
 * @fontsource ships the woff2 files inside the npm package, so `npm ci` is the
 * only network the build needs, and it already had to succeed for anything else
 * to work. Latin subset and the four weights actually used, to keep the payload
 * honest — the design uses 400/500/600/700 sans and 400/500 mono.
 */
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-sans/latin-700.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";

import "./globals.css";

export const metadata: Metadata = {
  // Was "SAP S/4HANA WRICEF Assistant", which contradicts the product: this works
  // against any tracking sheet, and the schema is detected per project.
  title: "MigrationBot — ask your tracking sheets",
  description:
    "Ask questions about a tracking spreadsheet in plain language, and make changes that are queued, attributed and audited.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full dark">
      <body className="flex h-full flex-col bg-ink-950 font-sans text-ink-100 antialiased">
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
