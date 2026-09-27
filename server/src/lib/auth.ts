import { SignJWT, jwtVerify } from "jose";
import { hash, compare } from "bcryptjs";
import { cookies } from "next/headers";

const secret = new TextEncoder().encode(
  process.env.JWT_SECRET || "fallback-secret"
);

export async function hashPassword(password: string): Promise<string> {
  return hash(password, 12);
}

export async function verifyPassword(
  password: string,
  hashed: string
): Promise<boolean> {
  return compare(password, hashed);
}

export async function createToken(
  payload: Record<string, unknown>,
  expiresIn = "7d"
): Promise<string> {
  return new SignJWT(payload)
    .setProtectedHeader({ alg: "HS256" })
    .setExpirationTime(expiresIn)
    .sign(secret);
}

export async function verifyToken(
  token: string
): Promise<Record<string, unknown>> {
  const { payload } = await jwtVerify(token, secret);
  return payload as Record<string, unknown>;
}

export async function getSessionFromCookie(): Promise<Record<string, unknown> | null> {
  try {
    const store = await cookies();
    const token = store.get("session")?.value;
    if (!token) return null;
    return await verifyToken(token);
  } catch {
    return null;
  }
}

export async function getSessionFromAuthHeader(
  request: Request
): Promise<Record<string, unknown> | null> {
  try {
    const auth = request.headers.get("authorization");
    if (!auth?.startsWith("Bearer ")) return null;
    const token = auth.slice(7);
    return await verifyToken(token);
  } catch {
    return null;
  }
}

export async function getAuthSession(
  request?: Request
): Promise<Record<string, unknown> | null> {
  const cookieSession = await getSessionFromCookie();
  if (cookieSession) return cookieSession;
  if (request) return getSessionFromAuthHeader(request);
  return null;
}
