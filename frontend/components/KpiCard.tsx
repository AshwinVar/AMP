export default function KpiCard({
  title,
  value,
  small = false,
}: {
  title: string;
  value: string | number;
  small?: boolean;
}) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 hover:border-slate-600 transition">
      <p className="text-slate-400 text-sm">{title}</p>
      <h3 className={`${small ? "text-xl" : "text-3xl"} font-bold mt-2`}>
        {value}
      </h3>
    </div>
  );
}
