"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { API_URL } from "../lib/api";

export default function LoginPage() {
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();

    setLoading(true);
    setError("");

    try {
      const res = await fetch(`${API_URL}/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          username,
          password
        })
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(
          data.detail ||
          data.error ||
          "Login failed"
        );
      }

      localStorage.setItem("token", data.access_token);
      localStorage.setItem("username", username);
      localStorage.setItem("role", data.role);
      localStorage.setItem("company", data.tenant || "DEFAULT");

      router.push("/dashboard");
    } catch (err: any) {
      setError(err.message || "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen bg-slate-950 flex items-center justify-center p-6">
      <div className="w-full max-w-md bg-slate-900 border border-slate-800 rounded-3xl p-8 shadow-2xl">
        <div className="mb-8 text-center">
          <p className="text-slate-400 text-sm">MES Lite SaaS MVP</p>
          <h1 className="text-4xl font-bold text-white mt-2">FlowMES</h1>
          <p className="text-slate-400 mt-2">Sign in to continue</p>
        </div>

        <form onSubmit={handleLogin} className="space-y-5">
          <input
            className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-white"
            placeholder="Username"
            value={username}
            onChange={(e) =>
              setUsername(e.target.value)
            }
            required
          />

          <input
            type="password"
            className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-white"
            placeholder="Password"
            value={password}
            onChange={(e) =>
              setPassword(e.target.value)
            }
            required
          />

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-3 text-red-400 text-sm">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-white text-slate-950 font-semibold py-3 hover:opacity-90 transition"
          >
            {loading ? "Signing in..." : "Login"}
          </button>
        </form>

        <div className="mt-6 text-center text-slate-500 text-xs">
          Accounts are created by your administrator.
        </div>
      </div>
    </main>
  );
}
