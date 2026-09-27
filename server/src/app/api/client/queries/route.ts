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
      .select({
        queryLimit: users.queryLimit,
        queriesUsed: users.queriesUsed,
        unlimitedUntil: users.unlimitedUntil,
      })
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
        queriesRemaining,
        hasUnlimited,
        queryLimit: user.queryLimit,
        queriesUsed: user.queriesUsed,
        unlimitedUntil: user.unlimitedUntil
          ? new Date(user.unlimitedUntil).toISOString()
          : null,
      },
    });
  } catch (error) {
    console.error("Queries GET error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session?.userId) {
      return NextResponse.json(
        { success: false, error: "Не авторизован" },
        { status: 401 }
      );
    }

    const userId = Number(session.userId);

    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, userId))
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

    // ТЗ 2.2: отправки безлимитчиков тоже считаются в «Отправлено» (queriesUsed).
    if (hasUnlimited) {
      await db
        .update(users)
        .set({ queriesUsed: user.queriesUsed + 1, updatedAt: new Date() })
        .where(eq(users.id, userId));
      return NextResponse.json({
        success: true,
        data: { queriesRemaining: -1, hasUnlimited: true },
      });
    }

    const remaining = user.queryLimit - user.queriesUsed;

    if (remaining <= 0) {
      return NextResponse.json(
        { success: false, error: "Запросы исчерпаны" },
        { status: 403 }
      );
    }

    await db
      .update(users)
      .set({ queriesUsed: user.queriesUsed + 1, updatedAt: new Date() })
      .where(eq(users.id, userId));

    return NextResponse.json({
      success: true,
      data: { queriesRemaining: remaining - 1, hasUnlimited: false },
    });
  } catch (error) {
    console.error("Queries POST error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

