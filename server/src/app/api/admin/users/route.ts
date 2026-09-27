import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users, userDataHistory, appSettings } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq, sql, ilike, or } from "drizzle-orm";
import { hashPassword } from "@/lib/auth";

async function requireAdmin(request: NextRequest) {
  const session = await getAuthSession(request);
  if (!session || session.role !== "admin") {
    return null;
  }
  return session;
}

export async function GET(request: NextRequest) {
  try {
    const session = await requireAdmin(request);
    if (!session) {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const url = new URL(request.url);
    const search = url.searchParams.get("search") || "";
    const id = url.searchParams.get("id");

    if (id) {
      const [user] = await db
        .select()
        .from(users)
        .where(eq(users.id, Number(id)))
        .limit(1);

      if (!user) {
        return NextResponse.json(
          { success: false, error: "Пользователь не найден" },
          { status: 404 }
        );
      }

      const history = await db
        .select()
        .from(userDataHistory)
        .where(eq(userDataHistory.userId, user.id))
        .orderBy(sql`${userDataHistory.changedAt} DESC`)
        .limit(200);

      return NextResponse.json({
        success: true,
        data: { user, history },
      });
    }

    let allUsers;
    if (search) {
      allUsers = await db
        .select()
        .from(users)
        .where(
          or(
            ilike(users.login, `%${search}%`),
            ilike(users.gameName, `%${search}%`),
            ilike(users.gameId, `%${search}%`)
          )
        )
        .orderBy(sql`${users.id} ASC`);
    } else {
      allUsers = await db
        .select()
        .from(users)
        .orderBy(sql`${users.id} ASC`);
    }

    return NextResponse.json({ success: true, data: allUsers });
  } catch (error) {
    console.error("Admin users GET error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

export async function PATCH(request: NextRequest) {
  try {
    const session = await requireAdmin(request);
    if (!session) {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const body = await request.json();
    const { userId, updates, batchAll, resetHwid } = body;

    if (batchAll) {
      const updateData: Record<string, unknown> = { updatedAt: new Date() };
      // «Лимит всем»: значение приводим к целому >= 0, мусор игнорируем.
      const rawLimit = Number(updates?.queryLimit);
      const limitAll =
        updates?.queryLimit !== undefined &&
        Number.isFinite(rawLimit) &&
        rawLimit >= 0
          ? Math.floor(rawLimit)
          : undefined;
      if (limitAll !== undefined) {
        updateData.queryLimit = limitAll;
        // Тот же лимит запоминаем как стартовый для будущих регистраций.
        await db
          .insert(appSettings)
          .values({ key: "defaultQueryLimit", value: String(limitAll) })
          .onConflictDoUpdate({
            target: appSettings.key,
            set: { value: String(limitAll), updatedAt: new Date() },
          });
      }
      if (updates.queriesUsed !== undefined) updateData.queriesUsed = updates.queriesUsed;
      if (updates.unlimitedUntil !== undefined) {
        updateData.unlimitedUntil = updates.unlimitedUntil
          ? new Date(updates.unlimitedUntil)
          : null;
      }
      await db.update(users).set(updateData);
      return NextResponse.json({
        success: true,
        data: { batchUpdated: true, defaultQueryLimit: limitAll ?? null },
      });
    }

    if (!userId) {
      return NextResponse.json(
        { success: false, error: "userId обязателен" },
        { status: 400 }
      );
    }

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

    const updateData: Record<string, unknown> = { updatedAt: new Date() };

    const trackedFields: Array<{ key: string; dbField: string }> = [
  { key: "login", dbField: "login" },
  { key: "gameName", dbField: "gameName" },
  { key: "gameId", dbField: "gameId" },
  { key: "gameFraction", dbField: "gameFraction" },
  { key: "gameRang", dbField: "gameRang" },
  { key: "gameDepartment", dbField: "gameDepartment" },
  { key: "gameJobTitle", dbField: "gameJobTitle" },
  { key: "selectedPrompt", dbField: "selectedPrompt" },
];

    for (const { key, dbField } of trackedFields) {
      if (updates[key] !== undefined) {
        const newValue = String(updates[key] || "").trim();
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
    }

    if (updates.queryLimit !== undefined) updateData.queryLimit = updates.queryLimit;
    if (updates.queriesUsed !== undefined) updateData.queriesUsed = updates.queriesUsed;

    if (updates.unlimitedUntil !== undefined) {
      updateData.unlimitedUntil = updates.unlimitedUntil
        ? new Date(updates.unlimitedUntil)
        : null;
    }

    if (updates.role !== undefined) updateData.role = updates.role;

    if (updates.password) {
      updateData.passwordHash = await hashPassword(updates.password);
    }

    if (resetHwid) {
      updateData.hwid = null;
      await db.insert(userDataHistory).values({
        userId,
        fieldName: "hwid",
        oldValue: existing.hwid || null,
        newValue: "RESET",
      });
    }

    await db
      .update(users)
      .set(updateData)
      .where(eq(users.id, userId));

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Admin users PATCH error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

export async function DELETE(request: NextRequest) {
  try {
    const session = await requireAdmin(request);
    if (!session) {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const url = new URL(request.url);
    const userId = url.searchParams.get("userId");
    if (!userId) {
      return NextResponse.json(
        { success: false, error: "userId обязателен" },
        { status: 400 }
      );
    }

    await db.delete(users).where(eq(users.id, Number(userId)));

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Admin users DELETE error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
