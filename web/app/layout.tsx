import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Finance Assistant — TBX",
  description: "Ask about vendor spend, payouts and reconciliation in plain language.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
