import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { verifyPassword, createToken } from "@/lib/auth";
import { eq, and, gt } from "drizzle-orm";

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

    const hasUnlimited =
      user.unlimitedUntil !== null &&
      new Date(user.unlimitedUntil) > new Date();
    const queriesRemaining = hasUnlimited
      ? -1
      : Math.max(0, user.queryLimit - user.queriesUsed);

    if (queriesRemaining === 0 && !hasUnlimited) {
      return NextResponse.json(
        { success: false, error: "Закончились запросы. Обратитесь к администратору." },
        { status: 403 }
      );
    }

    const token = await createToken(
      { userId: user.id, login: user.login, role: user.role },
      "30d"
    );

    return NextResponse.json({
      success: true,
      data: {
        token,
        user: {
          id: user.id,
          login: user.login,
          role: user.role,
          needsHwid: !user.hwid,
          hwidBound: !!user.hwid,
          gameName: user.gameName,
          gameId: user.gameId,
          gameFraction: user.gameFraction,
          gameRang: user.gameRang,
          gameDepartment: user.gameDepartment,
          gameJobTitle: user.gameJobTitle,
          queryLimit: user.queryLimit,
          queriesUsed: user.queriesUsed,
          queriesRemaining,
          hasUnlimited,
          unlimitedUntil: user.unlimitedUntil,
          selectedPrompt: user.selectedPrompt,
        },
      },
    });
  } catch (error) {
    console.error("Client auth error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}


