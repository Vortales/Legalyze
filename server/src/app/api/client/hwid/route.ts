import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users, userDataHistory } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq } from "drizzle-orm";

export async function POST(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session?.userId) {
      return NextResponse.json(
        { success: false, error: "Не авторизован" },
        { status: 401 }
      );
    }

    const body = await request.json();
    const { hwid } = body;

    if (!hwid) {
      return NextResponse.json(
        { success: false, error: "HWID обязателен" },
        { status: 400 }
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

    if (user.hwid && user.hwid !== hwid) {
      return NextResponse.json(
        { success: false, error: "HWID не совпадает. Обратитесь к администратору." },
        { status: 403 }
      );
    }

    if (!user.hwid) {
      await db
        .update(users)
        .set({ hwid, updatedAt: new Date() })
        .where(eq(users.id, userId));

      await db.insert(userDataHistory).values({
        userId,
        fieldName: "hwid",
        oldValue: null,
        newValue: hwid,
      });
    }

    return NextResponse.json({
      success: true,
      data: { hwidRegistered: true },
    });
  } catch (error) {
    console.error("HWID error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

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
      .select({ hwid: users.hwid })
      .from(users)
      .where(eq(users.id, Number(session.userId)))
      .limit(1);

    return NextResponse.json({
      success: true,
      data: { hwid: user?.hwid || null },
    });
  } catch {
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
