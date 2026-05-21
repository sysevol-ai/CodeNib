import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CodeMiner Wiki Demo",
  description: "Chat with a repository, powered by CodeMiner.",
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
