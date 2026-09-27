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
    const { template } = body;

    if (!template || typeof template !== "object") {
      return NextResponse.json(
        { success: false, error: "Некорректные данные шаблона" },
        { status: 400 }
      );
    }

    const userId = Number(session.userId);

    const [existing] = await db
      .select()
      .from(users)
      .where(eq(users.id, userId))
      .limit(1);

    if (!existing) {
      return NextResponse.json(
        { success: false, error: "Пользователь не найден" },
        { status: 404 }
      );
    }

    const fieldMapping: Record<string, keyof typeof template> = {
      gameName: "Name",
      gameId: "ID",
      gameFraction: "Fraction",
      gameRang: "Rang",
      gameDepartment: "Department",
      gameJobTitle: "JobTitle",
    };

    const updateData: Record<string, unknown> = { updatedAt: new Date() };

    for (const [dbField, templateKey] of Object.entries(fieldMapping)) {
      const newValue = String(template[templateKey] || "").trim();
      const oldValue = String((existing as Record<string, unknown>)[dbField] || "").trim();

      if (newValue !== oldValue) {
        await db.insert(userDataHistory).values({
          userId,
          fieldName: dbField,
          oldValue: oldValue || null,
          newValue: newValue || null,
        });
        updateData[dbField] = newValue || null;
      }
    }

    await db
      .update(users)
      .set(updateData)
      .where(eq(users.id, userId));

    return NextResponse.json({
      success: true,
      data: { synced: true },
    });
  } catch (error) {
    console.error("Client template sync error:", error);
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

    const userId = Number(session.userId);

    const [user] = await db
      .select({
        gameName: users.gameName,
        gameId: users.gameId,
        gameFraction: users.gameFraction,
        gameRang: users.gameRang,
        gameDepartment: users.gameDepartment,
        gameJobTitle: users.gameJobTitle,
      })
      .from(users)
      .where(eq(users.id, userId))
      .limit(1);

    if (!user) {
      return NextResponse.json(
        { success: false, error: "Пользователь не найден" },
        { status: 404 }
      );
    }

    return NextResponse.json({
      success: true,
      data: {
        template: {
          Name: user.gameName || "",
          ID: user.gameId || "",
          Fraction: user.gameFraction || "",
          Rang: user.gameRang || "",
          Department: user.gameDepartment || "",
          JobTitle: user.gameJobTitle || "",
        },
      },
    });
  } catch (error) {
    console.error("Client template GET error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

