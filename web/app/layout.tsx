import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "./globals.css";

export const metadata: Metadata = {
  title: "CodeNib Wiki · AI documentation you can talk to",
  description:
    "Browse AI-generated, source-grounded documentation for indexed repositories — and ask questions answered with citations, powered by CodeMiner.",
  icons: { icon: "/codenib-mark.png" },
};

// Set the theme before paint to avoid a flash of the wrong theme.
const themeInit = `(() => { try { const t = localStorage.getItem("cm-theme"); if (t === "dark" || t === "light") document.documentElement.dataset.theme = t; } catch (e) {} })();`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      data-theme="light"
      className={`${GeistSans.variable} ${GeistMono.variable}`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
