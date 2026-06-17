import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "TradeOS AI",
  description: "Quantitative crypto trading operating system",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

