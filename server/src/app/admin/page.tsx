"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";

type Tab = "dashboard" | "users" | "keys" | "prompts" | "updates";

interface Stats {
  totalUsers: number;
  adminCount: number;
  regularUsers: number;
  withHwid: number;
  activeUnlimited: number;
  totalQueriesUsed: number;
  activeKeys: number;
}

interface User {
  id: number;
  login: string;
  role: string;
  hwid: string | null;
  gameName: string | null;
  gameId: string | null;
  gameFraction: string | null;
  gameRang: string | null;
  gameDepartment: string | null;
  gameJobTitle: string | null;
  queryLimit: number;
  queriesUsed: number;
  unlimitedUntil: string | null;
  selectedPrompt: string | null;
  createdAt: string;
  updatedAt: string;
}

interface HistoryEntry {
  id: number;
  fieldName: string;
  oldValue: string | null;
  newValue: string | null;
  changedAt: string;
}

interface SubKey {
  id: number;
  keyCode: string;
  durationDays: number;
  maxUses: number;
  currentUses: number;
  isActive: boolean;
  createdAt: string;
}

interface PromptFile {
  id: number;
  filename: string;
  checksum: string | null;
  createdAt: string;
}

interface AppUpdate {
  id: number;
  version: string;
  originalName: string;
  description: string | null;
  isActive: boolean;
  createdAt: string;
}

const FIELD_LABELS: Record<string, string> = {
  login: "Логин",
  gameName: "Name",
  gameId: "ID",
  gameFraction: "Fraction",
  gameRang: "Rang",
  gameDepartment: "Department",
  gameJobTitle: "JobTitle",
  hwid: "HWID",
  selectedPrompt: "Промт",
};

function promptDisplayName(filename: string | null): string {
  if (!filename) return "—";
  let name = String(filename).trim().replace(/\\/g, "/").split("/").pop() || "";
  for (const ext of [".enc", ".bin", ".dat", ".txt", ".json", ".prompt", ".pdf", ".pdfenc"]) {
    if (name.toLowerCase().endsWith(ext)) {
      name = name.slice(0, -ext.length);
    }
  }
  return name || "—";
}

export default function AdminPage() {
  const router = useRouter();
  const [tab, setTab] = useState<Tab>("dashboard");
  const [adminUser, setAdminUser] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [stats, setStats] = useState<Stats | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [userSearch, setUserSearch] = useState("");
  const [selectedUser, setSelectedUser] = useState<User | null>(null);
  const [userHistory, setUserHistory] = useState<HistoryEntry[]>([]);
  const [editModal, setEditModal] = useState(false);
  const [editData, setEditData] = useState<Record<string, string | number | null>>({});

  // Безлимит диалог
  const [unlimitedModal, setUnlimitedModal] = useState(false);
  const [unlimitedUserId, setUnlimitedUserId] = useState<number | null>(null);
  const [unlimitedDays, setUnlimitedDays] = useState<string>("30");

  const [keys, setKeys] = useState<SubKey[]>([]);
  const [genDuration, setGenDuration] = useState(30);
  const [genMaxUses, setGenMaxUses] = useState(1);
  const [genCount, setGenCount] = useState(1);
  const [prompts, setPrompts] = useState<PromptFile[]>([]);
  const [updates, setUpdates] = useState<AppUpdate[]>([]);
  const [newVersion, setNewVersion] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState<"ok" | "err">("ok");

  const showMsg = useCallback((text: string, type: "ok" | "err" = "ok") => {
    setMsg(text);
    setMsgType(type);
    setTimeout(() => setMsg(""), 4000);
  }, []);

  useEffect(() => {
    fetch("/api/auth/me")
      .then((r) => r.json())
      .then((d) => {
        if (!d.success || d.data.role !== "admin") {
          router.push("/");
          return;
        }
        setAdminUser(d.data.login);
        setLoading(false);
      })
      .catch(() => router.push("/"));
  }, [router]);

  const loadStats = useCallback(async () => {
    const res = await fetch("/api/admin/stats");
    const data = await res.json();
    if (data.success) setStats(data.data);
  }, []);

  const loadUsers = useCallback(async () => {
    const q = userSearch ? `?search=${encodeURIComponent(userSearch)}` : "";
    const res = await fetch(`/api/admin/users${q}`);
    const data = await res.json();
    if (data.success) setUsers(data.data);
  }, [userSearch]);

  const loadKeys = useCallback(async () => {
    const res = await fetch("/api/admin/keys");
    const data = await res.json();
    if (data.success) setKeys(data.data);
  }, []);

  const loadPrompts = useCallback(async () => {
    const res = await fetch("/api/admin/prompts");
    const data = await res.json();
    if (data.success) setPrompts(data.data);
  }, []);

  const loadUpdates = useCallback(async () => {
    const res = await fetch("/api/admin/updates");
    const data = await res.json();
    if (data.success) setUpdates(data.data);
  }, []);

  useEffect(() => {
    if (loading) return;
    if (tab === "dashboard") loadStats();
    if (tab === "users") loadUsers();
    if (tab === "keys") loadKeys();
    if (tab === "prompts") loadPrompts();
    if (tab === "updates") loadUpdates();
  }, [tab, loading, loadStats, loadUsers, loadKeys, loadPrompts, loadUpdates]);

  async function openUser(userId: number) {
    const res = await fetch(`/api/admin/users?id=${userId}`);
    const data = await res.json();
    if (data.success) {
      setSelectedUser(data.data.user);
      setUserHistory(data.data.history);
      setEditData({
        login: data.data.user.login,
	    gameName: data.data.user.gameName || "",
	    gameId: data.data.user.gameId || "",
	    gameFraction: data.data.user.gameFraction || "",
	    gameRang: data.data.user.gameRang || "",
	    gameDepartment: data.data.user.gameDepartment || "",
	    gameJobTitle: data.data.user.gameJobTitle || "",
	   selectedPrompt: data.data.user.selectedPrompt || "",
	    queryLimit: data.data.user.queryLimit,
	    queriesUsed: data.data.user.queriesUsed,
	    role: data.data.user.role,
	    password: "",
      });
      setEditModal(true);
    }
  }

  async function saveUser() {
    if (!selectedUser) return;
    const updatesPayload: Record<string, string | number | null> = { ...editData };
    if (!updatesPayload.password) delete updatesPayload.password;

    const res = await fetch("/api/admin/users", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId: selectedUser.id, updates: updatesPayload }),
    });
    const data = await res.json();
    if (data.success) {
      showMsg("Пользователь обновлён");
      setEditModal(false);
      loadUsers();
    } else {
      showMsg(data.error, "err");
    }
  }

  async function resetHwid(userId: number) {
    const res = await fetch("/api/admin/users", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, updates: {}, resetHwid: true }),
    });
    const data = await res.json();
    if (data.success) {
      showMsg("HWID сброшен");
      loadUsers();
    } else {
      showMsg(data.error, "err");
    }
  }

  function openUnlimitedModal(userId: number) {
    setUnlimitedUserId(userId);
    setUnlimitedDays("30");
    setUnlimitedModal(true);
  }

  async function applyUnlimited() {
    if (!unlimitedUserId) return;
    const days = parseInt(unlimitedDays, 10);
    if (isNaN(days) || days < 1) {
      showMsg("Введите корректное количество дней (минимум 1)", "err");
      return;
    }
    const until = new Date(Date.now() + days * 86400000).toISOString();
    const res = await fetch("/api/admin/users", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId: unlimitedUserId, updates: { unlimitedUntil: until } }),
    });
    const data = await res.json();
    if (data.success) {
      showMsg(`Безлимит на ${days} дней установлен`);
      setUnlimitedModal(false);
      loadUsers();
    } else {
      showMsg(data.error, "err");
    }
  }

  async function removeUnlimited(userId: number) {
    const res = await fetch("/api/admin/users", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, updates: { unlimitedUntil: null } }),
    });
    const data = await res.json();
    if (data.success) {
      showMsg("Безлимит снят");
      loadUsers();
    }
  }

  async function deleteUser(userId: number) {
    if (!confirm("Удалить пользователя?")) return;
    const res = await fetch(`/api/admin/users?userId=${userId}`, { method: "DELETE" });
    const data = await res.json();
    if (data.success) {
      showMsg("Пользователь удалён");
      loadUsers();
    }
  }

  async function batchSetQueryLimit(limit: number) {
    const res = await fetch("/api/admin/users", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batchAll: true, updates: { queryLimit: limit } }),
    });
    const data = await res.json();
    if (data.success) {
      showMsg(`Лимит ${limit} установлен всем пользователям`);
      loadUsers();
    }
  }

  async function generateKeys() {
    const res = await fetch("/api/admin/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        durationDays: genDuration,
        maxUses: genMaxUses,
        count: genCount,
      }),
    });
    const data = await res.json();
    if (data.success) {
      showMsg(`Создано ключей: ${data.data.length}`);
      loadKeys();
    } else {
      showMsg(data.error, "err");
    }
  }

  async function toggleKey(keyId: number, isActive: boolean) {
    await fetch("/api/admin/keys", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyId, isActive }),
    });
    loadKeys();
  }

  async function deleteKey(keyId: number) {
    if (!confirm("Удалить ключ?")) return;
    await fetch(`/api/admin/keys?keyId=${keyId}`, { method: "DELETE" });
    loadKeys();
  }

  async function uploadPrompt(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/admin/prompts", { method: "POST", body: fd });
    const data = await res.json();
    if (data.success) {
      showMsg("Промт загружен");
      loadPrompts();
    } else {
      showMsg(data.error, "err");
    }
    e.target.value = "";
  }

  async function deletePrompt(id: number) {
    if (!confirm("Удалить промт?")) return;
    await fetch(`/api/admin/prompts?id=${id}`, { method: "DELETE" });
    showMsg("Промт удалён");
    loadPrompts();
  }

  async function uploadUpdate(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || !newVersion) {
      showMsg("Укажите версию и файл", "err");
      return;
    }
    if (uploading) return;
    setUploading(true);
    showMsg(`Загрузка ${file.name} (${(file.size / 1048576).toFixed(1)} МБ)… не закрывайте страницу`);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("version", newVersion);
      fd.append("description", newDesc);
      const res = await fetch("/api/admin/updates", { method: "POST", body: fd });
      // Прокси/сервер может ответить не JSON (413, 502, таймаут) — показываем как есть.
      let data: { success?: boolean; error?: string } | null = null;
      try {
        data = await res.json();
      } catch {
        data = null;
      }
      if (res.ok && data?.success) {
        showMsg("Обновление загружено");
        setNewVersion("");
        setNewDesc("");
        loadUpdates();
      } else {
        showMsg(data?.error || `Загрузка не удалась (HTTP ${res.status})`, "err");
      }
    } catch (err) {
      showMsg("Сеть оборвала загрузку: " + (err instanceof Error ? err.message : String(err)), "err");
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  }

  async function deleteUpdate(id: number) {
    if (!confirm("Удалить обновление?")) return;
    await fetch(`/api/admin/updates?id=${id}`, { method: "DELETE" });
    showMsg("Обновление удалено");
    loadUpdates();
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

  const tabs: { key: Tab; label: string; icon: string }[] = [
    { key: "dashboard", label: "Обзор", icon: "📊" },
    { key: "users", label: "Пользователи", icon: "👥" },
    { key: "keys", label: "Ключи", icon: "🔑" },
    { key: "prompts", label: "Промты", icon: "📄" },
    { key: "updates", label: "Обновления", icon: "📦" },
  ];

  return (
    <div className="min-h-screen bg-gray-950 flex">
      <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col shrink-0">
        <div className="p-4 border-b border-gray-800">
          <h1 className="text-lg font-bold bg-gradient-to-r from-indigo-400 to-purple-400 bg-clip-text text-transparent">
            Legalyze Admin
          </h1>
          <p className="text-xs text-gray-500 mt-1">{adminUser}</p>
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {tabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`w-full text-left px-3 py-2 rounded-lg text-sm flex items-center gap-2 transition-colors ${
                tab === t.key
                  ? "bg-indigo-600/20 text-indigo-400"
                  : "text-gray-400 hover:bg-gray-800 hover:text-white"
              }`}
            >
              <span>{t.icon}</span> {t.label}
            </button>
          ))}
        </nav>
        <div className="p-2 border-t border-gray-800">
          <button
            onClick={handleLogout}
            className="w-full text-left px-3 py-2 rounded-lg text-sm text-gray-400 hover:bg-gray-800 hover:text-white"
          >
            🚪 Выйти
          </button>
        </div>
      </aside>

      <main className="flex-1 overflow-auto p-6">
        {msg && (
          <div
            className={`mb-4 p-3 rounded-lg text-sm animate-fade-in ${
              msgType === "ok"
                ? "bg-emerald-500/10 border border-emerald-500/30 text-emerald-400"
                : "bg-red-500/10 border border-red-500/30 text-red-400"
            }`}
          >
            {msg}
          </div>
        )}

        {/* ===== DASHBOARD ===== */}
        {tab === "dashboard" && stats && (
          <div className="animate-fade-in">
            <h2 className="text-2xl font-bold text-white mb-6">Обзор системы</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {[
                { label: "Всего пользователей", value: stats.totalUsers, color: "text-indigo-400" },
                { label: "Администраторов", value: stats.adminCount, color: "text-purple-400" },
                { label: "С HWID", value: stats.withHwid, color: "text-cyan-400" },
                { label: "Безлимит активен", value: stats.activeUnlimited, color: "text-emerald-400" },
                { label: "Запросов использовано", value: stats.totalQueriesUsed, color: "text-amber-400" },
                { label: "Активных ключей", value: stats.activeKeys, color: "text-rose-400" },
                { label: "Обычных юзеров", value: stats.regularUsers, color: "text-sky-400" },
              ].map((s) => (
                <div key={s.label} className="bg-gray-900/80 border border-gray-800 rounded-xl p-4">
                  <p className="text-xs text-gray-500 uppercase tracking-wider">{s.label}</p>
                  <p className={`text-2xl font-bold mt-1 ${s.color}`}>{s.value}</p>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ===== USERS ===== */}
        {tab === "users" && (
          <div className="animate-fade-in">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-2xl font-bold text-white">Пользователи</h2>
              <div className="flex gap-2">
                <button
                  onClick={() => {
                    const v = prompt("Новый лимит запросов для всех:");
                    if (v) batchSetQueryLimit(Number(v));
                  }}
                  className="px-3 py-1.5 text-xs bg-amber-600/20 text-amber-400 border border-amber-600/30 rounded-lg hover:bg-amber-600/30"
                >
                  Лимит всем
                </button>
              </div>
            </div>

            <div className="mb-4">
              <input
                type="text"
                value={userSearch}
                onChange={(e) => setUserSearch(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && loadUsers()}
                placeholder="Поиск по логину, имени, ID..."
                className="w-full max-w-md px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white placeholder-gray-500 outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
				  <th className="p-2">ID</th>
				  <th className="p-2">Логин</th>
				  <th className="p-2">Роль</th>
				  <th className="p-2">Name</th>
				  <th className="p-2">Промт</th>
				  <th className="p-2">HWID</th>
				  <th className="p-2">Запросы</th>
				  <th className="p-2">Безлимит</th>
				  <th className="p-2">Отправлено</th>
				  <th className="p-2">Действия</th>
				</tr>
                </thead>
                <tbody>
				  {users.map((u) => (
					<tr key={u.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
					  {/* 1. ID */}
					  <td className="p-2 font-mono text-gray-400">{u.id}</td>
					  {/* 2. Логин */}
					  <td className="p-2 text-white">{u.login}</td>
					  {/* 3. Роль */}
					  <td className="p-2">
						<span className={`px-2 py-0.5 rounded text-xs ${u.role === "admin" ? "bg-purple-500/20 text-purple-400" : "bg-gray-700 text-gray-300"}`}>
						  {u.role}
						</span>
					  </td>
					  {/* 4. Name */}
					  <td className="p-2 text-gray-300 max-w-[120px] truncate">{u.gameName || "—"}</td>
					  {/* 5. Промт */}
					  <td className="p-2 text-xs text-cyan-400 max-w-[140px] truncate font-mono">
						{u.selectedPrompt ? promptDisplayName(u.selectedPrompt) : <span className="text-gray-600">—</span>}
					  </td>
					  {/* 6. HWID */}
					  <td className="p-2 font-mono text-xs text-gray-500 max-w-[80px] truncate">{u.hwid ? u.hwid.slice(0, 12) + "..." : "—"}</td>
					  {/* 7. Запросы */}
					  <td className="p-2 text-gray-300">{u.queriesUsed}/{u.queryLimit}</td>
					  {/* 8. Безлимит */}
					  <td className="p-2 text-xs">
						{u.unlimitedUntil && new Date(u.unlimitedUntil) > new Date() ? (
						  <span className="text-emerald-400">{new Date(u.unlimitedUntil).toLocaleDateString("ru-RU")}</span>
						) : (
						  <span className="text-gray-600">—</span>
						)}
					  </td>
					  {/* 9. Отправлено */}
					  <td className="p-2 text-gray-300 font-medium">{u.queriesUsed}</td>
					  {/* 10. Действия */}
					  <td className="p-2">
						<div className="flex gap-1 flex-wrap">
						  <button onClick={() => openUser(u.id)} className="px-2 py-1 text-xs bg-indigo-600/20 text-indigo-400 rounded hover:bg-indigo-600/30">✏️</button>
						  <button onClick={() => resetHwid(u.id)} className="px-2 py-1 text-xs bg-amber-600/20 text-amber-400 rounded hover:bg-amber-600/30" title="Сбросить HWID">🔑</button>
						  <button onClick={() => openUnlimitedModal(u.id)} className="px-2 py-1 text-xs bg-emerald-600/20 text-emerald-400 rounded hover:bg-emerald-600/30" title="Выдать безлимит">∞</button>
						  <button onClick={() => removeUnlimited(u.id)} className="px-2 py-1 text-xs bg-gray-600/20 text-gray-400 rounded hover:bg-gray-600/30" title="Снять безлимит">✕</button>
						  <button onClick={() => deleteUser(u.id)} className="px-2 py-1 text-xs bg-red-600/20 text-red-400 rounded hover:bg-red-600/30">🗑</button>
						</div>
					  </td>
					</tr>
				  ))}
				</tbody>
              </table>
            </div>
          </div>
        )}

        {/* ===== KEYS ===== */}
        {tab === "keys" && (
          <div className="animate-fade-in">
            <h2 className="text-2xl font-bold text-white mb-4">Ключи подписки</h2>
            <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-4 mb-6">
              <h3 className="text-sm font-medium text-gray-300 mb-3">Генерация ключей</h3>
              <div className="flex flex-wrap gap-3 items-end">
                <div>
                  <label className="text-xs text-gray-500">Дней</label>
                  <input type="number" value={genDuration} onChange={(e) => setGenDuration(Number(e.target.value))} min={1} className="block w-24 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm" />
                </div>
                <div>
                  <label className="text-xs text-gray-500">Макс. использований (0=∞)</label>
                  <input type="number" value={genMaxUses} onChange={(e) => setGenMaxUses(Number(e.target.value))} min={0} className="block w-24 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm" />
                </div>
                <div>
                  <label className="text-xs text-gray-500">Количество</label>
                  <input type="number" value={genCount} onChange={(e) => setGenCount(Number(e.target.value))} min={1} max={50} className="block w-24 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm" />
                </div>
                <button onClick={generateKeys} className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white text-sm rounded-lg">Сгенерировать</button>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-gray-500 border-b border-gray-800">
                    <th className="p-2">Ключ</th>
                    <th className="p-2">Дней</th>
                    <th className="p-2">Использований</th>
                    <th className="p-2">Статус</th>
                    <th className="p-2">Создан</th>
                    <th className="p-2">Действия</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                      <td className="p-2 font-mono text-xs text-indigo-400">{k.keyCode}</td>
                      <td className="p-2 text-gray-300">{k.durationDays}</td>
                      <td className="p-2 text-gray-300">{k.currentUses}/{k.maxUses === 0 ? "∞" : k.maxUses}</td>
                      <td className="p-2">
                        <span className={`px-2 py-0.5 rounded text-xs ${k.isActive ? "bg-emerald-500/20 text-emerald-400" : "bg-red-500/20 text-red-400"}`}>
                          {k.isActive ? "Активен" : "Неактивен"}
                        </span>
                      </td>
                      <td className="p-2 text-gray-500 text-xs">{new Date(k.createdAt).toLocaleDateString("ru-RU")}</td>
                      <td className="p-2 flex gap-1">
                        <button onClick={() => toggleKey(k.id, !k.isActive)} className="px-2 py-1 text-xs bg-amber-600/20 text-amber-400 rounded hover:bg-amber-600/30">
                          {k.isActive ? "Деактивировать" : "Активировать"}
                        </button>
                        <button onClick={() => deleteKey(k.id)} className="px-2 py-1 text-xs bg-red-600/20 text-red-400 rounded hover:bg-red-600/30">🗑</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ===== PROMPTS ===== */}
        {tab === "prompts" && (
          <div className="animate-fade-in">
            <h2 className="text-2xl font-bold text-white mb-4">Промт файлы</h2>
            <div className="mb-4">
              <label className="block px-4 py-6 border-2 border-dashed border-gray-700 rounded-xl text-center cursor-pointer hover:border-indigo-500 transition-colors">
                <span className="text-gray-400">Нажмите для загрузки зашифрованного промт-файла</span>
                <input type="file" onChange={uploadPrompt} className="hidden" />
              </label>
            </div>
            <div className="space-y-2">
              {prompts.map((p) => (
                <div key={p.id} className="flex items-center justify-between bg-gray-900/80 border border-gray-800 rounded-lg px-4 py-3">
                  <div>
                    <p className="text-white text-sm font-medium">{p.filename}</p>
                    <p className="text-xs text-gray-500">
                      {p.checksum?.slice(0, 16)}... • {new Date(p.createdAt).toLocaleDateString("ru-RU")}
                    </p>
                  </div>
                  <button onClick={() => deletePrompt(p.id)} className="px-3 py-1 text-xs bg-red-600/20 text-red-400 rounded hover:bg-red-600/30">Удалить</button>
                </div>
              ))}
              {prompts.length === 0 && <p className="text-gray-500 text-sm">Нет загруженных промтов</p>}
            </div>
          </div>
        )}

        {/* ===== UPDATES ===== */}
        {tab === "updates" && (
          <div className="animate-fade-in">
            <h2 className="text-2xl font-bold text-white mb-4">Обновления клиента</h2>
            <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-4 mb-6">
              <div className="flex flex-wrap gap-3 items-end">
                <div>
                  <label className="text-xs text-gray-500">Версия</label>
                  <input type="text" value={newVersion} onChange={(e) => setNewVersion(e.target.value)} placeholder="1.0.0" className="block w-32 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm" />
                </div>
                <div className="flex-1 min-w-[200px]">
                  <label className="text-xs text-gray-500">Описание</label>
                  <input type="text" value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Описание обновления" className="block w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm" />
                </div>
                <label className={`px-4 py-1.5 text-white text-sm rounded-lg ${uploading ? "bg-gray-600 cursor-wait" : "bg-indigo-600 hover:bg-indigo-700 cursor-pointer"}`}>
                  {uploading ? "Загрузка…" : "Загрузить файл"}
                  <input type="file" accept=".zip,.exe" disabled={uploading} onChange={uploadUpdate} className="hidden" />
                </label>
              </div>
            </div>
            <div className="space-y-2">
              {updates.map((u) => (
                <div key={u.id} className="flex items-center justify-between bg-gray-900/80 border border-gray-800 rounded-lg px-4 py-3">
                  <div>
                    <p className="text-white text-sm font-medium">
                      v{u.version} <span className="text-gray-500">— {u.originalName}</span>
                    </p>
                    {u.description && <p className="text-xs text-gray-400">{u.description}</p>}
                    <p className="text-xs text-gray-500">{new Date(u.createdAt).toLocaleDateString("ru-RU")} • {u.isActive ? "Активно" : "Неактивно"}</p>
                  </div>
                  <button onClick={() => deleteUpdate(u.id)} className="px-3 py-1 text-xs bg-red-600/20 text-red-400 rounded hover:bg-red-600/30">Удалить</button>
                </div>
              ))}
              {updates.length === 0 && <p className="text-gray-500 text-sm">Нет загруженных обновлений</p>}
            </div>
          </div>
        )}
      </main>

      {/* ===== МОДАЛКА: Безлимит ===== */}
      {unlimitedModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="bg-gray-900 border border-gray-800 rounded-2xl w-full max-w-sm animate-fade-in">
            <div className="p-6 border-b border-gray-800 flex items-center justify-between">
              <h3 className="text-lg font-bold text-white">Выдать безлимит</h3>
              <button onClick={() => setUnlimitedModal(false)} className="text-gray-400 hover:text-white text-xl">×</button>
            </div>
            <div className="p-6">
              <label className="block text-sm text-gray-400 mb-2">Количество дней безлимита:</label>
              <input
                type="number"
                min={1}
                value={unlimitedDays}
                onChange={(e) => setUnlimitedDays(e.target.value)}
                className="w-full px-4 py-2.5 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm outline-none focus:ring-2 focus:ring-indigo-500"
                placeholder="Введите количество дней"
                autoFocus
              />
              <div className="flex gap-2 mt-3">
                {[7, 14, 30, 60, 90, 180, 365].map((d) => (
                  <button
                    key={d}
                    onClick={() => setUnlimitedDays(String(d))}
                    className="px-2 py-1 text-xs bg-gray-800 text-gray-300 rounded hover:bg-indigo-600/30 hover:text-indigo-300 transition-colors"
                  >
                    {d}д
                  </button>
                ))}
              </div>
            </div>
            <div className="p-6 border-t border-gray-800 flex justify-end gap-3">
              <button onClick={() => setUnlimitedModal(false)} className="px-4 py-2 text-sm bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700">Отмена</button>
              <button onClick={applyUnlimited} className="px-4 py-2 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700">Выдать</button>
            </div>
          </div>
        </div>
      )}

      {/* ===== МОДАЛКА: Редактирование пользователя ===== */}
      {editModal && selectedUser && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="bg-gray-900 border border-gray-800 rounded-2xl w-full max-w-3xl max-h-[90vh] overflow-auto animate-fade-in">
            <div className="p-6 border-b border-gray-800 flex items-center justify-between">
              <h3 className="text-lg font-bold text-white">Редактирование: {selectedUser.login}</h3>
              <button onClick={() => setEditModal(false)} className="text-gray-400 hover:text-white text-xl">×</button>
            </div>
            <div className="p-6 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                {[
                  { key: "login", label: "Логин" },
                  { key: "password", label: "Новый пароль", type: "password" },
                  { key: "gameName", label: "Name" },
                  { key: "gameId", label: "ID" },
                  { key: "gameFraction", label: "Fraction" },
                  { key: "gameRang", label: "Rang" },
                  { key: "gameDepartment", label: "Department" },
                  { key: "gameJobTitle", label: "JobTitle" },
                ].map((f) => (
                  <div key={f.key}>
                    <label className="text-xs text-gray-500">{f.label}</label>
                    <input
                      type={f.type || "text"}
                      value={String(editData[f.key] ?? "")}
                      onChange={(e) => setEditData({ ...editData, [f.key]: e.target.value })}
                      className="block w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm"
                    />
                  </div>
                ))}
                <div>
                  <label className="text-xs text-gray-500">Роль</label>
                  <select
                    value={String(editData.role ?? "user")}
                    onChange={(e) => setEditData({ ...editData, role: e.target.value })}
                    className="block w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm"
                  >
                    <option value="user">user</option>
                    <option value="admin">admin</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500">Лимит запросов</label>
                  <input
                    type="number"
                    value={Number(editData.queryLimit ?? 0)}
                    onChange={(e) => setEditData({ ...editData, queryLimit: Number(e.target.value) })}
                    className="block w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm"
                  />
                </div>
                <div>
                  <label className="text-xs text-gray-500">Использовано</label>
                  <input
                    type="number"
                    value={Number(editData.queriesUsed ?? 0)}
                    onChange={(e) => setEditData({ ...editData, queriesUsed: Number(e.target.value) })}
                    className="block w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-white text-sm"
                  />
                </div>
              </div>

							{/* ── Выбранный промт (read-only) ── */}
			  <div className="bg-gray-800/40 border border-gray-700 rounded-xl p-4 mt-4">
			    <div className="flex items-center justify-between mb-2">
				  <label className="text-xs text-gray-400 uppercase tracking-wider font-medium">
				    📄 Выбранный промт
				  </label>
				  {editData.selectedPrompt && (
				    <button
					  onClick={() => setEditData({ ...editData, selectedPrompt: "" })}
					  className="text-xs text-red-400 hover:text-red-300 px-2 py-0.5 rounded hover:bg-red-500/10 transition-colors"
				    >
					  Сбросить
				    </button>
				  )}
			    </div>
			    <div className="flex items-center gap-3">
				  <div className="flex-1 px-4 py-2.5 bg-gray-900 border border-gray-700 rounded-lg">
				    <span className="text-white text-sm font-mono">
					  {promptDisplayName(String(editData.selectedPrompt || ""))}
				    </span>
				  </div>
				  <div className="text-xs text-gray-500 shrink-0">
				    {editData.selectedPrompt ? (
					  <span className="text-emerald-400">● выбран</span>
				    ) : (
					  <span className="text-gray-600">○ не выбран</span>
				    )}
				  </div>
			    </div>
			    <p className="text-xs text-gray-500 mt-2">
			  	  Промт выбирается пользователем в клиенте. Здесь отображается только для просмотра.
			    </p>
			  </div>

              {/* История изменений — табличный вид */}
              {userHistory.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-gray-300 mb-3">История изменений ({userHistory.length})</h4>
                  <div className="max-h-52 overflow-auto border border-gray-800 rounded-lg">
                    <table className="w-full text-xs">
                      <thead className="sticky top-0 bg-gray-800">
                        <tr className="text-left text-gray-400">
                          <th className="p-2 font-medium">Дата</th>
                          <th className="p-2 font-medium">Поле</th>
                          <th className="p-2 font-medium">Было</th>
                          <th className="p-2 font-medium">Стало</th>
                        </tr>
                      </thead>
                      <tbody>
                        {userHistory.map((h) => (
                          <tr key={h.id} className="border-t border-gray-800/50 hover:bg-gray-800/30">
                            <td className="p-2 text-gray-500 whitespace-nowrap">
                              {new Date(h.changedAt).toLocaleString("ru-RU", {
                                day: "2-digit",
                                month: "2-digit",
                                year: "2-digit",
                                hour: "2-digit",
                                minute: "2-digit",
                              })}
                            </td>
                            <td className="p-2 font-medium">
							  {h.fieldName === "selectedPrompt" ? (
								<span className="text-cyan-400">📄 {FIELD_LABELS[h.fieldName]}</span>
							  ) : (
								<span className="text-indigo-400">{FIELD_LABELS[h.fieldName] || h.fieldName}</span>
							  )}
							</td>
                            <td className="p-2 text-red-400/80 line-through max-w-[150px] truncate">
                              {h.oldValue || "пусто"}
                            </td>
                            <td className="p-2 text-emerald-400 max-w-[150px] truncate">
                              {h.newValue || "пусто"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
            <div className="p-6 border-t border-gray-800 flex justify-end gap-3">
              <button onClick={() => setEditModal(false)} className="px-4 py-2 text-sm bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700">Отмена</button>
              <button onClick={saveUser} className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700">Сохранить</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
