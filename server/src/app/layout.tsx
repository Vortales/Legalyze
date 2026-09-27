import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Legalyze — AI Assistant for Majestic RP",
  description: "Сервис AI-помощника для игроков Majestic RP",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ru">
      <body className="bg-gray-950 text-gray-100 antialiased min-h-screen">
        {children}
      </body>
    </html>
  );
}
