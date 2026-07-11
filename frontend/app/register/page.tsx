"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { API_URL } from "../../lib/api";

export default function RegisterPage() {
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("Admin");

  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();

    setLoading(true);
    setError("");

    try {
      const res = await fetch(`${API_URL}/register`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          username,
          password,
          role
        })
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(
          data.detail ||
          data.error ||
          "Registration failed"
        );
      }

      alert("Account created!");
      router.push("/");
    } catch (err: any) {
      setError(err.message || "Registration failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen bg-slate-950 flex items-center justify-center p-6">
      <div className="w-full max-w-md bg-slate-900 border border-slate-800 rounded-3xl p-8">
        <div className="mb-8 text-center">
          <h1 className="text-4xl font-bold text-white">
            Create Account
          </h1>

          <p className="text-slate-400 mt-2">
            Register for AMP
          </p>
        </div>

        <form
          onSubmit={handleRegister}
          className="space-y-5"
        >
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

          <select
            className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-white"
            value={role}
            onChange={(e) =>
              setRole(e.target.value)
            }
          >
            <option>Admin</option>
            <option>Supervisor</option>
            <option>Operator</option>
          </select>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-3 text-red-400 text-sm">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-white text-slate-950 font-semibold py-3"
          >
            {loading ? "Creating..." : "Register"}
          </button>
        </form>

        <div className="mt-6 text-center text-slate-400 text-sm">
          Already have an account?{" "}
          <button
            onClick={() =>
              router.push("/")
            }
            className="text-white underline"
          >
            Login
          </button>
        </div>
      </div>
    </main>
  );
}
