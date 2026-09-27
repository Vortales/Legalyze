import {
  pgTable,
  serial,
  varchar,
  text,
  integer,
  boolean,
  timestamp,
} from "drizzle-orm/pg-core";

export const users = pgTable("users", {
  id: serial("id").primaryKey(),
  login: varchar("login", { length: 50 }).unique().notNull(),
  passwordHash: varchar("password_hash", { length: 255 }).notNull(),
  role: varchar("role", { length: 20 }).default("user").notNull(),
  hwid: varchar("hwid", { length: 512 }),
  gameName: varchar("game_name", { length: 100 }),
  gameId: varchar("game_id", { length: 50 }),
  gameFraction: varchar("game_fraction", { length: 50 }),
  gameRang: varchar("game_rang", { length: 50 }),
  gameDepartment: varchar("game_department", { length: 100 }),
  gameJobTitle: varchar("game_job_title", { length: 200 }),
  queryLimit: integer("query_limit").default(10).notNull(),
  queriesUsed: integer("queries_used").default(0).notNull(),
  unlimitedUntil: timestamp("unlimited_until"),
  selectedPrompt: varchar("selected_prompt", { length: 255 }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});

export const userDataHistory = pgTable("user_data_history", {
  id: serial("id").primaryKey(),
  userId: integer("user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  fieldName: varchar("field_name", { length: 50 }).notNull(),
  oldValue: text("old_value"),
  newValue: text("new_value"),
  changedAt: timestamp("changed_at").defaultNow().notNull(),
});

export const subscriptionKeys = pgTable("subscription_keys", {
  id: serial("id").primaryKey(),
  keyCode: varchar("key_code", { length: 64 }).unique().notNull(),
  durationDays: integer("duration_days").notNull(),
  maxUses: integer("max_uses").default(1).notNull(),
  currentUses: integer("current_uses").default(0).notNull(),
  isActive: boolean("is_active").default(true).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

export const keyUsages = pgTable("key_usages", {
  id: serial("id").primaryKey(),
  keyId: integer("key_id")
    .references(() => subscriptionKeys.id, { onDelete: "cascade" })
    .notNull(),
  userId: integer("user_id")
    .references(() => users.id, { onDelete: "cascade" })
    .notNull(),
  usedAt: timestamp("used_at").defaultNow().notNull(),
});

export const promptFiles = pgTable("prompt_files", {
  id: serial("id").primaryKey(),
  filename: varchar("filename", { length: 255 }).notNull(),
  storedName: varchar("stored_name", { length: 255 }).notNull(),
  checksum: varchar("checksum", { length: 64 }),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

export const appUpdates = pgTable("app_updates", {
  id: serial("id").primaryKey(),
  version: varchar("version", { length: 30 }).notNull(),
  storedName: varchar("stored_name", { length: 255 }).notNull(),
  originalName: varchar("original_name", { length: 255 }).notNull(),
  description: text("description"),
  isActive: boolean("is_active").default(true).notNull(),
  createdAt: timestamp("created_at").defaultNow().notNull(),
});

// Key-value настройки приложения. defaultQueryLimit — лимит запросов,
// который назначается каждому новому пользователю при регистрации
// (задаётся кнопкой «Лимит всем» в админке, «Пользователи»).
export const appSettings = pgTable("app_settings", {
  key: varchar("key", { length: 100 }).primaryKey(),
  value: text("value").notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});
