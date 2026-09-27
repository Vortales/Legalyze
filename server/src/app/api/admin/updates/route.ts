import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { appUpdates } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq, sql } from "drizzle-orm";
import { unlink, mkdir } from "fs/promises";
import { createWriteStream } from "fs";
import { pipeline } from "stream/promises";
import { Readable } from "stream";
import Busboy from "busboy";
import path from "path";
import { randomUUID } from "crypto";

const UPDATES_DIR = path.join(process.cwd(), "data", "updates");
// Раздача = exe + portable chromium, zip большой — льём потоком на диск,
// а не в память (иначе сервер умирал без ошибки). Лимит 1 ГБ.
const MAX_UPLOAD_BYTES = 1024 * 1024 * 1024;
const ALLOWED_EXTS = new Set([".zip", ".exe"]);

export async function GET(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session || session.role !== "admin") {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const updates = await db
      .select()
      .from(appUpdates)
      .orderBy(sql`${appUpdates.createdAt} DESC`);

    return NextResponse.json({ success: true, data: updates });
  } catch (error) {
    console.error("Admin updates GET error:", error);
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

    await mkdir(UPDATES_DIR, { recursive: true });

    if (!request.body) {
      return NextResponse.json(
        { success: false, error: "Файл и версия обязательны" },
        { status: 400 }
      );
    }

    // Потоковый разбор multipart (busboy): файл пишется на диск по кускам
    // и НИКОГДА не держится в памяти целиком. formData()/arrayBuffer()
    // здесь использовать нельзя — 250 МБ в памяти убивают Node на
    // маленьком VPS (в логе это "upstream prematurely closed").
    const fields: Record<string, string> = {};
    let originalName = "";
    let filePath = "";
    let storedName = "";
    let gotFile = false;
    let tooLarge = false;
    const fileWrites: Promise<void>[] = [];

    try {
      await new Promise<void>((resolve, reject) => {
        const bb = Busboy({
          headers: { "content-type": request.headers.get("content-type") ?? "" },
          limits: { files: 1, fields: 10, fileSize: MAX_UPLOAD_BYTES },
        });
        bb.on("file", (name, stream, info) => {
          if (name !== "file") {
            stream.resume();
            return;
          }
          const ext = path.extname(info.filename || "").toLowerCase();
          if (!ALLOWED_EXTS.has(ext)) {
            stream.resume();
            reject(new Error("BAD_EXT:" + (ext || "без расширения")));
            return;
          }
          gotFile = true;
          originalName = info.filename || `update${ext}`;
          storedName = `${randomUUID()}${ext}`;
          filePath = path.join(UPDATES_DIR, storedName);
          stream.on("limit", () => {
            tooLarge = true;
          });
          fileWrites.push(pipeline(stream, createWriteStream(filePath)));
        });
        bb.on("field", (name, val) => {
          fields[name] = String(val);
        });
        bb.on("error", (err: unknown) =>
          reject(err instanceof Error ? err : new Error(String(err)))
        );
        bb.on("finish", () => resolve());
        Readable.fromWeb(request.body as never).pipe(bb);
      });
      await Promise.all(fileWrites);
    } catch (err) {
      if (filePath) {
        try {
          await unlink(filePath);
        } catch {
          // ignore
        }
      }
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.startsWith("BAD_EXT:")) {
        return NextResponse.json(
          { success: false, error: "Нужен файл .zip (или .exe), получен: " + msg.slice(8) },
          { status: 400 }
        );
      }
      throw err;
    }

    if (tooLarge) {
      try {
        await unlink(filePath);
      } catch {
        // ignore
      }
      return NextResponse.json(
        { success: false, error: "Файл больше 1 ГБ — такой не загрузить" },
        { status: 413 }
      );
    }

    const version = fields["version"] || "";
    const description = fields["description"] || "";
    if (!gotFile || !version) {
      if (filePath) {
        try {
          await unlink(filePath);
        } catch {
          // ignore
        }
      }
      return NextResponse.json(
        { success: false, error: "Файл и версия обязательны" },
        { status: 400 }
      );
    }

    const [newUpdate] = await db
      .insert(appUpdates)
      .values({
        version,
        storedName,
        originalName,
        description: description || null,
      })
      .returning();

    return NextResponse.json({ success: true, data: newUpdate });
  } catch (error) {
    console.error("Admin updates POST error:", error);
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

    if (update) {
      try {
        await unlink(path.join(UPDATES_DIR, update.storedName));
      } catch {
        // ignore
      }
    }

    await db.delete(appUpdates).where(eq(appUpdates.id, Number(updateId)));
    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Admin updates DELETE error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}
