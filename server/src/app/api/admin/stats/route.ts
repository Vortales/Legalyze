import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users, subscriptionKeys } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { sql, eq, and, gt } from "drizzle-orm";

export async function GET(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session || session.role !== "admin") {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const totalUsersResult = await db
      .select({ count: sql<number>`count(*)` })
      .from(users);
    const totalUsers = Number(totalUsersResult[0]?.count ?? 0);

    const adminCountResult = await db
      .select({ count: sql<number>`count(*)` })
      .from(users)
      .where(eq(users.role, "admin"));
    const adminCount = Number(adminCountResult[0]?.count ?? 0);

    const withHwidResult = await db
      .select({ count: sql<number>`count(*)` })
      .from(users)
      .where(sql`${users.hwid} IS NOT NULL`);
    const withHwid = Number(withHwidResult[0]?.count ?? 0);

    const now = new Date();
    const unlimitedResult = await db
      .select({ count: sql<number>`count(*)` })
      .from(users)
      .where(
        and(
          sql`${users.unlimitedUntil} IS NOT NULL`,
          gt(users.unlimitedUntil, now)
        )
      );
    const activeUnlimited = Number(unlimitedResult[0]?.count ?? 0);

    const totalQueriesResult = await db
      .select({ total: sql<number>`sum(${users.queriesUsed})` })
      .from(users);
    const totalQueriesUsed = Number(totalQueriesResult[0]?.total ?? 0);

    const keysResult = await db
      .select({ count: sql<number>`count(*)` })
      .from(subscriptionKeys)
      .where(eq(subscriptionKeys.isActive, true));
    const activeKeys = Number(keysResult[0]?.count ?? 0);

    return NextResponse.json({
      success: true,
      data: {
        totalUsers,
        adminCount,
        regularUsers: totalUsers - adminCount,
        withHwid,
        activeUnlimited,
        totalQueriesUsed,
        activeKeys,
      },
    });
  } catch (error) {
    console.error("Admin stats error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
