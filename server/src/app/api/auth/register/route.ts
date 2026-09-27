import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users, appSettings } from "@/db/schema";
import { hashPassword, createToken } from "@/lib/auth";
import { eq, sql } from "drizzle-orm";

// Стартовый лимит для новых пользователей (задаётся кнопкой «Лимит всем»).
// Если настройка не задана или таблица ещё не создана — 10, как раньше.
async function getDefaultQueryLimit(): Promise<number> {
  try {
    const [row] = await db
      .select()
      .from(appSettings)
      .where(eq(appSettings.key, "defaultQueryLimit"))
      .limit(1);
    const v = Number(row?.value);
    if (Number.isFinite(v) && v > 0) return Math.floor(v);
  } catch {
    // app_settings отсутствует (миграция не применена) — молча используем 10
  }
  return 10;
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { login, password } = body;

    if (!login || !password) {
      return NextResponse.json(
        { success: false, error: "Логин и пароль обязательны" },
        { status: 400 }
      );
    }

    if (login.length < 3 || login.length > 50) {
      return NextResponse.json(
        { success: false, error: "Логин должен быть от 3 до 50 символов" },
        { status: 400 }
      );
    }

    if (password.length < 6) {
      return NextResponse.json(
        { success: false, error: "Пароль должен быть не менее 6 символов" },
        { status: 400 }
      );
    }

    const existing = await db
      .select()
      .from(users)
      .where(eq(users.login, login))
      .limit(1);

    if (existing.length > 0) {
      return NextResponse.json(
        { success: false, error: "Пользователь с таким логином уже существует" },
        { status: 409 }
      );
    }

    const countResult = await db
      .select({ count: sql<number>`count(*)` })
      .from(users);
    const userCount = Number(countResult[0]?.count ?? 0);
    const role = userCount === 0 ? "admin" : "user";

    const passwordHash = await hashPassword(password);

    const [newUser] = await db
      .insert(users)
      .values({
        login,
        passwordHash,
        role,
        queryLimit: await getDefaultQueryLimit(),
        queriesUsed: 0,
      })
      .returning();

    const token = await createToken({
      userId: newUser.id,
      login: newUser.login,
      role: newUser.role,
    });

    const response = NextResponse.json({
      success: true,
      data: {
        user: {
          id: newUser.id,
          login: newUser.login,
          role: newUser.role,
        },
        token,
      },
    });

    response.cookies.set("session", token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      maxAge: 60 * 60 * 24 * 7,
      path: "/",
    });

    return response;
  } catch (error) {
    console.error("Register error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
