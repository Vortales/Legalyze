"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";

interface UserData {
  id: number;
  login: string;
  role: string;
  gameName: string | null;
  gameId: string | null;
  gameFraction: string | null;
  gameRang: string | null;
  gameDepartment: string | null;
  gameJobTitle: string | null;
  queryLimit: number;
  queriesUsed: number;
  queriesRemaining: number;
  hasUnlimited: boolean;
  unlimitedUntil: string | null;
  selectedPrompt: string | null;
}

export default function DashboardPage() {
  const router = useRouter();
  const [user, setUser] = useState<UserData | null>(null);
  const [loading, setLoading] = useState(true);
  const [keyCode, setKeyCode] = useState("");
  const [keyMsg, setKeyMsg] = useState("");
  const [keyLoading, setKeyLoading] = useState(false);
  const [latestVersion, setLatestVersion] = useState<string | null>(null);
  const [updateId, setUpdateId] = useState<number | null>(null);

  const loadUser = useCallback(async () => {
    try {
      const res = await fetch("/api/auth/me");
      const data = await res.json();
      if (!data.success) {
        router.push("/");
        return;
      }
      setUser(data.data);
    } catch {
      router.push("/");
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    loadUser();
    fetch("/api/client/update")
      .then((r) => r.json())
      .then((d) => {
        if (d.success && d.data.hasUpdate) {
          setLatestVersion(d.data.version);
          setUpdateId(d.data.updateId);
        }
      })
      .catch(() => {});
  }, [loadUser]);

  async function handleActivateKey(e: React.FormEvent) {
    e.preventDefault();
    setKeyMsg("");
    setKeyLoading(true);
    try {
      const res = await fetch("/api/keys/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: keyCode }),
      });
      const data = await res.json();
      if (data.success) {
        setKeyMsg(`Ключ активирован! Безлимит до ${new Date(data.data.unlimitedUntil).toLocaleDateString("ru-RU")}`);
        setKeyCode("");
        loadUser();
      } else {
        setKeyMsg(data.error);
      }
    } catch {
      setKeyMsg("Ошибка подключения");
    } finally {
      setKeyLoading(false);
    }
  }

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/");
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-950">
        <div className="animate-spin w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (!user) return null;

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-indigo-950">
      <header className="border-b border-gray-800 bg-gray-900/50 backdrop-blur-sm">
        <div className="max-w-5xl mx-auto px-4 py-4 flex items-center justify-between">
          <h1 className="text-xl font-bold bg-gradient-to-r from-indigo-400 to-purple-400 bg-clip-text text-transparent">
            Legalyze
          </h1>
          <div className="flex items-center gap-4">
            <span className="text-sm text-gray-400">{user.login}</span>
            <button
              onClick={handleLogout}
              className="px-3 py-1.5 text-sm bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors text-gray-300"
            >
              Выйти
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-8 space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-5">
            <p className="text-sm text-gray-400 mb-1">Запросы</p>
            {user.hasUnlimited ? (
              <p className="text-3xl font-bold text-emerald-400">∞</p>
            ) : (
              <p className="text-3xl font-bold text-white">
                {user.queriesRemaining}
                <span className="text-lg text-gray-500"> / {user.queryLimit}</span>
              </p>
            )}
            {user.hasUnlimited && user.unlimitedUntil && (
              <p className="text-xs text-gray-500 mt-1">
                до {new Date(user.unlimitedUntil).toLocaleDateString("ru-RU")}
              </p>
            )}
          </div>

          <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-5">
            <p className="text-sm text-gray-400 mb-1">Статус</p>
            <p className={`text-lg font-semibold ${user.hasUnlimited ? "text-emerald-400" : "text-indigo-400"}`}>
              {user.hasUnlimited ? "Безлимит" : "Стандарт"}
            </p>
          </div>

          <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-5">
            <p className="text-sm text-gray-400 mb-1">Промт</p>
            <p className="text-sm text-white font-mono truncate">
              {user.selectedPrompt || "Не выбран"}
            </p>
          </div>
        </div>

        {user.gameName && (
          <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-5">
            <h2 className="text-lg font-semibold text-white mb-3">Игровые данные</h2>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
              <div>
                <span className="text-gray-500">Name:</span>
                <span className="ml-2 text-white">{user.gameName}</span>
              </div>
              <div>
                <span className="text-gray-500">ID:</span>
                <span className="ml-2 text-white">{user.gameId}</span>
              </div>
              <div>
                <span className="text-gray-500">Fraction:</span>
                <span className="ml-2 text-white">{user.gameFraction}</span>
              </div>
              <div>
                <span className="text-gray-500">Rang:</span>
                <span className="ml-2 text-white">{user.gameRang}</span>
              </div>
              <div>
                <span className="text-gray-500">Department:</span>
                <span className="ml-2 text-white">{user.gameDepartment}</span>
              </div>
              <div>
                <span className="text-gray-500">JobTitle:</span>
                <span className="ml-2 text-white">{user.gameJobTitle}</span>
              </div>
            </div>
          </div>
        )}

        <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-5">
          <h2 className="text-lg font-semibold text-white mb-3">Активация ключа подписки</h2>
          <form onSubmit={handleActivateKey} className="flex gap-3">
            <input
              type="text"
              value={keyCode}
              onChange={(e) => setKeyCode(e.target.value)}
              placeholder="LGL-XXXX-XXXX-XXXX-XXXXXXXX"
              className="flex-1 px-4 py-2.5 bg-gray-800 border border-gray-700 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-transparent outline-none text-white placeholder-gray-500 font-mono text-sm"
            />
            <button
              type="submit"
              disabled={keyLoading}
              className="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-medium rounded-lg transition-colors whitespace-nowrap"
            >
              {keyLoading ? "..." : "Активировать"}
            </button>
          </form>
          {keyMsg && (
            <p className={`mt-2 text-sm ${keyMsg.includes("активирован") ? "text-emerald-400" : "text-red-400"}`}>
              {keyMsg}
            </p>
          )}
        </div>

        {latestVersion && (
          <div className="bg-gray-900/80 border border-indigo-800 rounded-xl p-5">
            <h2 className="text-lg font-semibold text-white mb-2">Доступно обновление</h2>
            <p className="text-sm text-gray-400 mb-3">
              Версия: <span className="text-indigo-400 font-mono">{latestVersion}</span>
            </p>
            <a
              href={`/api/client/update?action=download&id=${updateId}`}
              className="inline-block px-6 py-2.5 bg-emerald-600 hover:bg-emerald-700 text-white font-medium rounded-lg transition-colors text-sm"
            >
              Скачать обновление
            </a>
          </div>
        )}
      </main>
    </div>
  );
}
