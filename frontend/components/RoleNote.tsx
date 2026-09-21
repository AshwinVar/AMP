/**
 * Where a write control was withheld by role, say so — in the place the control
 * would have been, so the screen does not simply look broken to an Operator.
 * The server's rule is the reason (lib/roles.ts); this only states it.
 */
export default function RoleNote({ children }: { children: React.ReactNode }) {
  return (
    <p role="note" className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 text-sm text-slate-500">
      {children}
    </p>
  );
}
