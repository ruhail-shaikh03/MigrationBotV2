import type { Metadata } from "next";
import { IBM_Plex_Sans, IBM_Plex_Mono } from "next/font/google";
import { SessionProvider } from "next-auth/react";
import "./globals.css";

/**
 * IBM Plex, not Inter or Geist. Plex was drawn for enterprise data tooling, which
 * is exactly what this is; it holds up at the small sizes tables need, and its
 * tabular figures matter for a product that is mostly numbers in columns. The
 * mono cut carries every sheet coordinate, ID and count in the UI.
 */
const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

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
      <body
        className={`${plexSans.variable} ${plexMono.variable} flex h-full flex-col bg-ink-950 font-sans text-ink-100 antialiased`}
      >
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
