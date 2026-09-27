-- Миграция 0001: таблица настроек приложения (PostgreSQL).
-- Хранит defaultQueryLimit — стартовый лимит для новых регистраций
-- (задаётся кнопкой «Лимит всем» в админке). Безопасно запускать
-- повторно: IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS "app_settings" (
  "key" varchar(100) PRIMARY KEY,
  "value" text NOT NULL,
  "updated_at" timestamp DEFAULT now() NOT NULL
);
