import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { SessionProvider } from "next-auth/react";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "MigrationBot | SAP S/4HANA WRICEF Assistant",
  description: "AI conversational assistant and queue-backed automation manager for SAP S/4HANA WRICEF migrations.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full dark">
      <body className={`${geistSans.variable} ${geistMono.variable} h-full font-sans antialiased bg-[#030014] text-zinc-100 flex flex-col`}>
        <SessionProvider>
          {children}
        </SessionProvider>
      </body>
    </html>
  );
}
