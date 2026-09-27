import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { subscriptionKeys } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq, sql } from "drizzle-orm";
import { randomBytes } from "crypto";

function generateKeyCode(): string {
  const raw = randomBytes(24).toString("base64url");
  return `LGL-${raw.slice(0, 4)}-${raw.slice(4, 8)}-${raw.slice(8, 12)}-${raw.slice(12, 20)}`.toUpperCase();
}

export async function GET(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session || session.role !== "admin") {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const keys = await db
      .select()
      .from(subscriptionKeys)
      .orderBy(sql`${subscriptionKeys.createdAt} DESC`);

    return NextResponse.json({ success: true, data: keys });
  } catch (error) {
    console.error("Admin keys GET error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session || session.role !== "admin") {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const body = await request.json();
    const { durationDays, maxUses, count = 1 } = body;

    if (!durationDays || durationDays < 1) {
      return NextResponse.json(
        { success: false, error: "Длительность должна быть не менее 1 дня" },
        { status: 400 }
      );
    }

    const maxCount = Math.min(count, 50);
    const generatedKeys = [];

    for (let i = 0; i < maxCount; i++) {
      const keyCode = generateKeyCode();
      const [newKey] = await db
        .insert(subscriptionKeys)
        .values({
          keyCode,
          durationDays,
          maxUses: maxUses ?? 1,
        })
        .returning();
      generatedKeys.push(newKey);
    }

    return NextResponse.json({
      success: true,
      data: generatedKeys,
    });
  } catch (error) {
    console.error("Admin keys POST error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

export async function PATCH(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session || session.role !== "admin") {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const body = await request.json();
    const { keyId, isActive } = body;

    if (!keyId) {
      return NextResponse.json(
        { success: false, error: "keyId обязателен" },
        { status: 400 }
      );
    }

    await db
      .update(subscriptionKeys)
      .set({ isActive: isActive ?? false })
      .where(eq(subscriptionKeys.id, keyId));

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Admin keys PATCH error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

export async function DELETE(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session || session.role !== "admin") {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const url = new URL(request.url);
    const keyId = url.searchParams.get("keyId");

    if (!keyId) {
      return NextResponse.json(
        { success: false, error: "keyId обязателен" },
        { status: 400 }
      );
    }

    await db
      .delete(subscriptionKeys)
      .where(eq(subscriptionKeys.id, Number(keyId)));

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Admin keys DELETE error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
