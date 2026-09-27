import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { promptFiles, users, userDataHistory } from "@/db/schema";
import { getAuthSession } from "@/lib/auth";
import { eq } from "drizzle-orm";
import { readFile } from "fs/promises";
import path from "path";

const PROMPTS_DIR = path.join(process.cwd(), "data", "prompts");

function promptDisplayName(filename: string): string {
  let name = (filename || "").trim().replace(/\\/g, "/").split("/").pop() || "";
  for (const ext of [".enc", ".bin", ".dat", ".txt", ".json", ".prompt", ".pdf"]) {
    if (name.toLowerCase().endsWith(ext)) {
      name = name.slice(0, -ext.length);
    }
  }
  return name || "prompt";
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
    const filename = url.searchParams.get("filename");

    if (filename) {
      const [file] = await db
        .select()
        .from(promptFiles)
        .where(eq(promptFiles.filename, filename))
        .limit(1);

      if (!file) {
        return NextResponse.json(
          { success: false, error: "Файл не найден" },
          { status: 404 }
        );
      }

      const filePath = path.join(PROMPTS_DIR, file.storedName);
      const data = await readFile(filePath);

      return new NextResponse(data, {
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Disposition": `attachment; filename="${filename}"`,
          "X-Checksum": file.checksum || "",
        },
      });
    }

    const files = await db.select().from(promptFiles);
    return NextResponse.json({ success: true, data: files });
  } catch (error) {
    console.error("Prompts error:", error);
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

    const body = await request.json();
    const { filename } = body;

    if (!filename) {
      return NextResponse.json(
        { success: false, error: "Имя файла обязательно" },
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

    const oldValue = existing.selectedPrompt || "";
    const newValue = filename || "";

    if (oldValue !== newValue) {
      await db.insert(userDataHistory).values({
        userId,
        fieldName: "selectedPrompt",
        oldValue: oldValue ? promptDisplayName(oldValue) : null,
        newValue: newValue ? promptDisplayName(newValue) : null,
      });
    }

    await db
      .update(users)
      .set({ selectedPrompt: filename, updatedAt: new Date() })
      .where(eq(users.id, userId));

    return NextResponse.json({ success: true, data: { selectedPrompt: filename } });
  } catch (error) {
    console.error("Select prompt error:", error);
    return NextResponse.json(
      { success: false, error: "Ошибка сервера" },
      { status: 500 }
    );
  }
}

