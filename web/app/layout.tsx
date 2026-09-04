import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Finsight",
  description:
    "Grounded answers from your ledger. Every figure computed in SQL and "
    + "verified against the source rows before it is shown.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
