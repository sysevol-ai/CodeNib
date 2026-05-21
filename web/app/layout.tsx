import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CodeMiner · Code QA",
  description:
    "Ask questions about a repository and get answers grounded in its code, powered by CodeMiner.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
