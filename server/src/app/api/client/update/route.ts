import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { appUpdates } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq, desc } from "drizzle-orm";
import { createReadStream } from "fs";
import { stat } from "fs/promises";
import { Readable } from "stream";
import path from "path";

const UPDATES_DIR = path.join(process.cwd(), "data", "updates");

function parseVersion(v: string): number[] {
  return String(v)
    .split(".")
    .map((part) => parseInt(part, 10) || 0);
}

/**
 * Семантическое сравнение версий:
 *  1  — a новее b
 *  0  — версии равны
 * -1  — a старее b
 */
function compareVersions(a: string, b: string): number {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const va = pa[i] ?? 0;
    const vb = pb[i] ?? 0;
    if (va > vb) return 1;
    if (va < vb) return -1;
  }
  return 0;
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

    const url = new URL(request.url);
    const action = url.searchParams.get("action");
    const version = url.searchParams.get("version");

    if (action === "download") {
      const updateId = url.searchParams.get("id");
      if (!updateId) {
        return NextResponse.json(
          { success: false, error: "ID обновления обязателен" },
          { status: 400 }
        );
      }
      const [update] = await db
        .select()
        .from(appUpdates)
        .where(eq(appUpdates.id, Number(updateId)))
        .limit(1);
      if (!update) {
        return NextResponse.json(
          { success: false, error: "Обновление не найдено" },
          { status: 404 }
        );
      }
      const filePath = path.join(UPDATES_DIR, update.storedName);
      // Потоковая отдача: большой zip не грузится в память целиком.
      let size = 0;
      try {
        size = (await stat(filePath)).size;
      } catch {
        return NextResponse.json(
          { success: false, error: "Файл обновления не найден на диске" },
          { status: 404 }
        );
      }
      const webStream = Readable.toWeb(createReadStream(filePath));
      return new NextResponse(webStream as never, {
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Disposition": `attachment; filename="${update.originalName}"`,
          "Content-Length": String(size),
          "X-Version": update.version,
        },
      });
    }

    const [latest] = await db
      .select()
      .from(appUpdates)
      .where(eq(appUpdates.isActive, true))
      .orderBy(desc(appUpdates.createdAt))
      .limit(1);

    if (!latest) {
      return NextResponse.json({
        success: true,
        data: { hasUpdate: false },
      });
    }

    // Клиент сообщил свою версию и она >= серверной — обновления НЕТ
    if (version && compareVersions(version, latest.version) >= 0) {
      return NextResponse.json({
        success: true,
        data: { hasUpdate: false },
      });
    }

    return NextResponse.json({
      success: true,
      data: {
        hasUpdate: true,
        version: latest.version,
        updateId: latest.id,
        description: latest.description,
        originalName: latest.originalName,
      },
    });
  } catch (error) {
    console.error("Update error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
