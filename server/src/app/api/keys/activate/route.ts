import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { subscriptionKeys, keyUsages, users } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq, and } from "drizzle-orm";

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
    const { key } = body;

    if (!key) {
      return NextResponse.json(
        { success: false, error: "Ключ обязателен" },
        { status: 400 }
      );
    }

    const userId = Number(session.userId);

    const [subKey] = await db
      .select()
      .from(subscriptionKeys)
      .where(
        and(
          eq(subscriptionKeys.keyCode, key),
          eq(subscriptionKeys.isActive, true)
        )
      )
      .limit(1);

    if (!subKey) {
      return NextResponse.json(
        { success: false, error: "Неверный или неактивный ключ" },
        { status: 400 }
      );
    }

    if (subKey.maxUses > 0 && subKey.currentUses >= subKey.maxUses) {
      return NextResponse.json(
        { success: false, error: "Ключ исчерпан" },
        { status: 400 }
      );
    }

    const existingUsage = await db
      .select()
      .from(keyUsages)
      .where(
        and(eq(keyUsages.keyId, subKey.id), eq(keyUsages.userId, userId))
      )
      .limit(1);

    if (existingUsage.length > 0) {
      return NextResponse.json(
        { success: false, error: "Вы уже использовали этот ключ" },
        { status: 400 }
      );
    }

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

    const now = new Date();
    const currentExpiry = user.unlimitedUntil && new Date(user.unlimitedUntil) > now
      ? new Date(user.unlimitedUntil)
      : now;
    const newExpiry = new Date(
      currentExpiry.getTime() + subKey.durationDays * 24 * 60 * 60 * 1000
    );

    await db
      .update(users)
      .set({ unlimitedUntil: newExpiry, updatedAt: new Date() })
      .where(eq(users.id, userId));

    await db.insert(keyUsages).values({
      keyId: subKey.id,
      userId,
    });

    if (subKey.maxUses > 0) {
      await db
        .update(subscriptionKeys)
        .set({ currentUses: subKey.currentUses + 1 })
        .where(eq(subscriptionKeys.id, subKey.id));
    }

    return NextResponse.json({
      success: true,
      data: {
        unlimitedUntil: newExpiry.toISOString(),
        durationDays: subKey.durationDays,
      },
    });
  } catch (error) {
    console.error("Key activate error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
