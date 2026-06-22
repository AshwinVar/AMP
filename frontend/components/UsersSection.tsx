import React, { useState } from "react";
import type { User } from "../lib/types";
import { INPUT_CLASS } from "../lib/utils";

export default function UsersSection({
  users,
  company,
  addEmployee,
  updateUserRole,
  deleteUser,
  resetPassword,
}: {
  users: User[];
  company: string;
  addEmployee: (username: string, password: string, role: string) => Promise<void>;
  updateUserRole: (id: number, role: string) => void;
  deleteUser: (id: number) => void;
  resetPassword: (id: number, password: string) => Promise<void>;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("Operator");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [resetId, setResetId] = useState<number | null>(null);
  const [resetPw, setResetPw] = useState("");
  const [resetMsg, setResetMsg] = useState("");

  async function submitReset(id: number) {
    setResetMsg("");
    try {
      await resetPassword(id, resetPw);
      setResetId(null);
      setResetPw("");
      setResetMsg(`Password updated for user #${id}`);
    } catch (err: any) {
      setResetMsg(err?.message?.replace(/^PATCH .* failed: \d+ /, "") || "Reset failed");
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await addEmployee(username.trim(), password, role);
      setUsername("");
      setPassword("");
      setRole("Operator");
    } catch (err: any) {
      setError(err?.message?.replace(/^POST \/users failed: \d+ /, "") || "Failed to add employee");
    } finally {
      setBusy(false);
    }
  }

  const companyLabel = company === "GMATS" ? "GMATS Compressors" : "Default Factory";

  return (
    <section className="mt-8 space-y-6">
      <div>
        <div className="flex items-center gap-3">
          <h3 className="text-3xl font-bold">User Management</h3>
          <span className="rounded-lg bg-indigo-500/20 border border-indigo-500/40 px-3 py-1 text-xs text-indigo-300 font-semibold tracking-wider">
            {companyLabel}
          </span>
        </div>
        <p className="text-slate-400 mt-2 text-sm">
          Add and manage employees for your company. New employees can only sign in with the role you assign — they cannot self-register.
        </p>
      </div>

      <form onSubmit={submit} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 gap-3">
        <input
          className={INPUT_CLASS}
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
        />
        <input
          className={INPUT_CLASS}
          type="password"
          placeholder="Temporary password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <select className={INPUT_CLASS} value={role} onChange={(e) => setRole(e.target.value)}>
          <option>Operator</option>
          <option>Supervisor</option>
          <option>Admin</option>
        </select>
        <button type="submit" disabled={busy} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3 disabled:opacity-50">
          {busy ? "Adding…" : "Add Employee"}
        </button>
        {error && <p className="md:col-span-4 text-red-400 text-sm">{error}</p>}
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <div className="flex items-center justify-between mb-4">
          <h4 className="text-lg font-semibold">Employees <span className="text-slate-500 text-sm font-normal">({users.length})</span></h4>
          {resetMsg && <span className="text-green-400 text-sm">{resetMsg}</span>}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">ID</th>
                <th className="py-3 px-4">Username</th>
                <th className="py-3 px-4">Role</th>
                <th className="py-3 px-4">Change Role</th>
                <th className="py-3 px-4">Action</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} className="border-b border-slate-800">
                  <td className="py-3 px-4">{user.id}</td>
                  <td className="py-3 px-4 font-medium">{user.username}</td>
                  <td className="py-3 px-4">{user.role}</td>
                  <td className="py-3 px-4">
                    <select className={INPUT_CLASS} value={user.role} onChange={(e) => updateUserRole(user.id, e.target.value)}>
                      <option>Admin</option>
                      <option>Supervisor</option>
                      <option>Operator</option>
                    </select>
                  </td>
                  <td className="py-3 px-4">
                    {resetId === user.id ? (
                      <div className="flex items-center gap-2">
                        <input
                          type="text"
                          className="w-40 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-sm"
                          placeholder="New password"
                          value={resetPw}
                          onChange={(e) => setResetPw(e.target.value)}
                          autoFocus
                        />
                        <button onClick={() => submitReset(user.id)} className="text-xs text-green-400 border border-green-500/30 rounded-lg px-2 py-1 hover:bg-green-500/10">Save</button>
                        <button onClick={() => { setResetId(null); setResetPw(""); }} className="text-xs text-slate-400 border border-slate-700 rounded-lg px-2 py-1">Cancel</button>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2">
                        <button onClick={() => { setResetId(user.id); setResetPw(""); setResetMsg(""); }} className="rounded-xl border border-slate-600 px-3 py-2 text-slate-300 hover:bg-slate-800 text-xs">Reset PW</button>
                        <button onClick={() => deleteUser(user.id)} className="rounded-xl border border-red-500/40 px-3 py-2 text-red-400 hover:bg-red-500/10 text-xs">Delete</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-6 px-4 text-slate-400">No employees yet — add one above.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
