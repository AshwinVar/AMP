import type { User } from "../lib/types";
import { INPUT_CLASS } from "../lib/utils";

export default function UsersSection({
  users,
  updateUserRole,
  deleteUser,
}: {
  users: User[];
  updateUserRole: (id: number, role: string) => void;
  deleteUser: (id: number) => void;
}) {
  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-6">
      <h3 className="text-2xl font-semibold mb-2">User Management</h3>

      <p className="text-slate-400 mb-6">
        Admin-only user role management.
      </p>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-slate-400 border-b border-slate-800">
            <tr>
              <th className="py-3">ID</th>
              <th>Username</th>
              <th>Role</th>
              <th>Update Role</th>
              <th>Action</th>
            </tr>
          </thead>

          <tbody>
            {users.map((user) => (
              <tr key={user.id} className="border-b border-slate-800">
                <td className="py-3">{user.id}</td>
                <td>{user.username}</td>
                <td>{user.role}</td>
                <td>
                  <select className={INPUT_CLASS} value={user.role} onChange={(e) => updateUserRole(user.id, e.target.value)}>
                    <option>Admin</option>
                    <option>Supervisor</option>
                    <option>Operator</option>
                  </select>
                </td>
                <td>
                  <button onClick={() => deleteUser(user.id)} className="rounded-xl border border-red-500/40 px-4 py-2 text-red-400 hover:bg-red-500/10">
                    Delete
                  </button>
                </td>
              </tr>
            ))}

            {users.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 text-slate-400">No users found.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
