import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq } from "drizzle-orm";

export async function GET(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session?.userId) {
      return NextResponse.json(
        { success: false, error: "Не авторизован" },
        { status: 401 }
      );
    }

    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, Number(session.userId)))
      .limit(1);

    if (!user) {
      return NextResponse.json(
        { success: false, error: "Пользователь не найден" },
        { status: 404 }
      );
    }

    const hasUnlimited =
      user.unlimitedUntil !== null &&
      new Date(user.unlimitedUntil) > new Date();
    const queriesRemaining = hasUnlimited
      ? -1
      : Math.max(0, user.queryLimit - user.queriesUsed);

    return NextResponse.json({
      success: true,
      data: {
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
        queriesRemaining,
        hasUnlimited,
        unlimitedUntil: user.unlimitedUntil,
        selectedPrompt: user.selectedPrompt,
        createdAt: user.createdAt,
      },
    });
  } catch (error) {
    console.error("Me error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
