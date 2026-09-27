"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";

type Tab = "login" | "register";

export default function HomePage() {
  const router = useRouter();
  const [tab, setTab] = useState<Tab>("login");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [latestVersion, setLatestVersion] = useState<string | null>(null);
  const [updateId, setUpdateId] = useState<number | null>(null);

  useEffect(() => {
    fetch("/api/auth/me")
      .then((r) => r.json())
      .then((d) => {
        if (d.success) {
          if (d.data.role === "admin") router.push("/admin");
          else router.push("/dashboard");
        }
      })
      .catch(() => {});
    fetch("/api/client/update")
      .then((r) => r.json())
      .then((d) => {
        if (d.success && d.data.hasUpdate) {
          setLatestVersion(d.data.version);
          setUpdateId(d.data.updateId);
        }
      })
      .catch(() => {});
  }, [router]);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password, remember }),
      });
      const data = await res.json();
      if (!data.success) {
        setError(data.error);
        return;
      }
      if (data.data.user.role === "admin") router.push("/admin");
      else router.push("/dashboard");
    } catch {
      setError("Ошибка подключения к серверу");
    } finally {
      setLoading(false);
    }
  }

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      });
      const data = await res.json();
      if (!data.success) {
        setError(data.error);
        return;
      }
      if (data.data.user.role === "admin") router.push("/admin");
      else router.push("/dashboard");
    } catch {
      setError("Ошибка подключения к серверу");
    } finally {
      setLoading(false);
    }
  }

  function handleDownload() {
    if (updateId) {
      window.open(`/api/client/update?action=download&id=${updateId}`, "_blank");
    }
  }

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-4 py-12 bg-gradient-to-br from-gray-950 via-gray-900 to-indigo-950">
      <div className="w-full max-w-md animate-fade-in">
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold bg-gradient-to-r from-indigo-400 to-purple-400 bg-clip-text text-transparent">
            Legalyze
          </h1>
          <p className="text-gray-400 mt-2 text-sm">AI-помощник для Majestic RP</p>
        </div>

        <div className="bg-gray-900/80 backdrop-blur-sm rounded-2xl border border-gray-800 shadow-2xl p-6">
          <div className="flex mb-6 bg-gray-800 rounded-lg p-1">
            <button
              onClick={() => { setTab("login"); setError(""); }}
              className={`flex-1 py-2 px-4 rounded-md text-sm font-medium transition-all ${
                tab === "login"
                  ? "bg-indigo-600 text-white shadow"
                  : "text-gray-400 hover:text-white"
              }`}
            >
              Войти
            </button>
            <button
              onClick={() => { setTab("register"); setError(""); }}
              className={`flex-1 py-2 px-4 rounded-md text-sm font-medium transition-all ${
                tab === "register"
                  ? "bg-indigo-600 text-white shadow"
                  : "text-gray-400 hover:text-white"
              }`}
            >
              Регистрация
            </button>
          </div>

          {error && (
            <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
              {error}
            </div>
          )}

          <form onSubmit={tab === "login" ? handleLogin : handleRegister}>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-300 mb-1">
                  Логин
                </label>
                <input
                  type="text"
                  value={login}
                  onChange={(e) => setLogin(e.target.value)}
                  className="w-full px-4 py-2.5 bg-gray-800 border border-gray-700 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-transparent outline-none text-white placeholder-gray-500"
                  placeholder="Введите логин"
                  required
                  minLength={3}
                  maxLength={50}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-300 mb-1">
                  Пароль
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-4 py-2.5 bg-gray-800 border border-gray-700 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-transparent outline-none text-white placeholder-gray-500"
                  placeholder="Введите пароль"
                  required
                  minLength={6}
                />
              </div>

              {tab === "login" && (
                <label className="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={remember}
                    onChange={(e) => setRemember(e.target.checked)}
                    className="w-4 h-4 rounded bg-gray-800 border-gray-600 text-indigo-500 focus:ring-indigo-500"
                  />
                  Запомнить меня
                </label>
              )}

              <button
                type="submit"
                disabled={loading}
                className="w-full py-2.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-medium rounded-lg transition-colors"
              >
                {loading
                  ? "Загрузка..."
                  : tab === "login"
                  ? "Войти"
                  : "Зарегистрироваться"}
              </button>
            </div>
          </form>
        </div>

        <div className="mt-6 bg-gray-900/80 backdrop-blur-sm rounded-2xl border border-gray-800 shadow-2xl p-6">
          <h2 className="text-lg font-semibold text-white mb-2">Скачать клиент</h2>
          {latestVersion ? (
            <>
              <p className="text-sm text-gray-400 mb-4">
                Актуальная версия: <span className="text-indigo-400 font-mono">{latestVersion}</span>
              </p>
              <button
                onClick={handleDownload}
                className="w-full py-2.5 bg-emerald-600 hover:bg-emerald-700 text-white font-medium rounded-lg transition-colors"
              >
                Скачать Legalyze
              </button>
            </>
          ) : (
            <p className="text-sm text-gray-500">
              Клиент пока недоступен. Обратитесь к администратору.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
