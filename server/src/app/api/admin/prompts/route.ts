import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { promptFiles } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq, sql } from "drizzle-orm";
import { writeFile, unlink, mkdir } from "fs/promises";
import { createHash } from "crypto";
import path from "path";
import { randomUUID } from "crypto";

const PROMPTS_DIR = path.join(process.cwd(), "data", "prompts");

export async function GET(request: NextRequest) {
  try {
    const session = await getAuthSession(request);
    if (!session || session.role !== "admin") {
      return NextResponse.json(
        { success: false, error: "Доступ запрещён" },
        { status: 403 }
      );
    }

    const files = await db
      .select()
      .from(promptFiles)
      .orderBy(sql`${promptFiles.createdAt} DESC`);

    return NextResponse.json({ success: true, data: files });
  } catch (error) {
    console.error("Admin prompts GET error:", error);
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

    await mkdir(PROMPTS_DIR, { recursive: true });

    const formData = await request.formData();
    const file = formData.get("file") as File | null;

    if (!file) {
      return NextResponse.json(
        { success: false, error: "Файл обязателен" },
        { status: 400 }
      );
    }

    const buffer = Buffer.from(await file.arrayBuffer());
    const checksum = createHash("sha256").update(buffer).digest("hex");
    const ext = path.extname(file.name);
    const storedName = `${randomUUID()}${ext}`;
    const filePath = path.join(PROMPTS_DIR, storedName);

    await writeFile(filePath, buffer);

    const [newFile] = await db
      .insert(promptFiles)
      .values({
        filename: file.name,
        storedName,
        checksum,
      })
      .returning();

    return NextResponse.json({ success: true, data: newFile });
  } catch (error) {
    console.error("Admin prompts POST error:", error);
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
    const fileId = url.searchParams.get("id");

    if (!fileId) {
      return NextResponse.json(
        { success: false, error: "ID файла обязателен" },
        { status: 400 }
      );
    }

    const [file] = await db
      .select()
      .from(promptFiles)
      .where(eq(promptFiles.id, Number(fileId)))
      .limit(1);

    if (file) {
      try {
        await unlink(path.join(PROMPTS_DIR, file.storedName));
      } catch {
        // ignore
      }
    }

    await db.delete(promptFiles).where(eq(promptFiles.id, Number(fileId)));
    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Admin prompts DELETE error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}


