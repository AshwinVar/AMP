import type { ComplianceDocument, DocumentAnalytics } from "../lib/mega-pack1-types";

function statusStyle(status: string) {
  switch (status) {
    case "Approved": return "border-green-500/40 bg-green-500/10 text-green-300";
    case "Under Review": return "border-blue-500/40 bg-blue-500/10 text-blue-300";
    case "Obsolete": return "border-slate-500/40 bg-slate-500/10 text-slate-300";
    default: return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  }
}

export default function DocumentsSection({
  documents, analytics, form, setForm, createDocument, updateDocument, deleteDocument, generateReviewEscalations,
}: {
  documents: ComplianceDocument[];
  analytics: DocumentAnalytics | null;
  form: any;
  setForm: (value: any) => void;
  createDocument: (e: React.FormEvent) => void;
  updateDocument: (id: number, approval_status: string, version?: string) => void;
  deleteDocument?: (id: number) => void;
  generateReviewEscalations: () => void;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Documents & Compliance</h2>
          <p className="text-slate-400 mt-2">SOPs, work instructions, approvals, version control and review due tracking.</p>
        </div>
        <button onClick={generateReviewEscalations} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Generate Review Escalations</button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-6 gap-4">
        <Kpi title="Documents" value={analytics?.total_documents ?? 0} />
        <Kpi title="Draft" value={analytics?.draft ?? 0} />
        <Kpi title="Approved" value={analytics?.approved ?? 0} />
        <Kpi title="Review" value={analytics?.under_review ?? 0} />
        <Kpi title="Due" value={analytics?.review_due ?? 0} />
        <Kpi title="Obsolete" value={analytics?.obsolete ?? 0} />
      </div>

      <form onSubmit={createDocument} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-9 gap-4">
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Document No" value={form.document_no} onChange={(e) => setForm({ ...form, document_no: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.document_type} onChange={(e) => setForm({ ...form, document_type: e.target.value })}>
          <option>SOP</option><option>Work Instruction</option><option>Quality Manual</option><option>Checklist</option><option>Policy</option>
        </select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Department" value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Version" value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Owner" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} required />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.approval_status} onChange={(e) => setForm({ ...form, approval_status: e.target.value })}>
          <option>Draft</option><option>Under Review</option><option>Approved</option><option>Obsolete</option>
        </select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="date" value={form.review_due_date} onChange={(e) => setForm({ ...form, review_due_date: e.target.value })} required />
        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Add Doc</button>
      </form>

      <DataTable headers={["Doc", "Title", "Type", "Dept", "Version", "Owner", "Review Due", "Status", "Actions"]}>
        {documents.map((row) => (
          <tr key={row.id} className="border-b border-slate-800">
            <td className="py-3 px-4 font-semibold">{row.document_no}</td>
            <td className="py-3 px-4">{row.title}</td>
            <td className="py-3 px-4">{row.document_type}</td>
            <td className="py-3 px-4">{row.department}</td>
            <td className="py-3 px-4">{row.version}</td>
            <td className="py-3 px-4">{row.owner}</td>
            <td className="py-3 px-4">{row.review_due_date}</td>
            <td className="py-3 px-4"><select className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${statusStyle(row.approval_status)}`} value={row.approval_status} onChange={(e) => updateDocument(row.id, e.target.value, row.version)}><option>Draft</option><option>Under Review</option><option>Approved</option><option>Obsolete</option></select></td>
            <td className="py-3 px-4"><button onClick={() => deleteDocument?.(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1">Delete</button></td>
          </tr>
        ))}
      </DataTable>
    </section>
  );
}

function Kpi({ title, value }: { title: string; value: string | number }) { return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>; }
function DataTable({ headers, children }: { headers: string[]; children: React.ReactNode }) { return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><div className="overflow-x-auto rounded-xl border border-slate-800"><table className="w-full min-w-[1100px] text-left text-sm"><thead className="text-slate-400 border-b border-slate-800"><tr>{headers.map((h) => <th key={h} className="py-3 px-4">{h}</th>)}</tr></thead><tbody>{children}</tbody></table></div></div>; }
