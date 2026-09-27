import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { verifyPassword, createToken } from "@/lib/auth";
import { eq } from "drizzle-orm";

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { login, password, remember } = body;

    if (!login || !password) {
      return NextResponse.json(
        { success: false, error: "Логин и пароль обязательны" },
        { status: 400 }
      );
    }

    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.login, login))
      .limit(1);

    if (!user) {
      return NextResponse.json(
        { success: false, error: "Неверный логин или пароль" },
        { status: 401 }
      );
    }

    const valid = await verifyPassword(password, user.passwordHash);
    if (!valid) {
      return NextResponse.json(
        { success: false, error: "Неверный логин или пароль" },
        { status: 401 }
      );
    }

    const expiresIn = remember ? "30d" : "7d";
    const maxAge = remember ? 60 * 60 * 24 * 30 : 60 * 60 * 24 * 7;

    const token = await createToken(
      {
        userId: user.id,
        login: user.login,
        role: user.role,
      },
      expiresIn
    );

    const response = NextResponse.json({
      success: true,
      data: {
        user: {
          id: user.id,
          login: user.login,
          role: user.role,
          hwid: user.hwid,
          gameName: user.gameName,
          gameId: user.gameId,
          gameFraction: user.gameFraction,
          gameRang: user.gameRang,
          gameDepartment: user.gameDepartment,
          gameJobTitle: user.gameJobTitle,
          queryLimit: user.queryLimit,
          queriesUsed: user.queriesUsed,
          unlimitedUntil: user.unlimitedUntil,
          selectedPrompt: user.selectedPrompt,
        },
        token,
      },
    });

    response.cookies.set("session", token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      maxAge,
      path: "/",
    });

    return response;
  } catch (error) {
    console.error("Login error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
