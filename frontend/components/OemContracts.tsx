"use client";

import React, { useMemo, useState } from "react";

import {
  contractError,
  startMonthOf,
  termsTemplate,
  type ContractDetail,
  type Terms,
} from "../lib/contracts";
import type { FleetMachine } from "../lib/oem";
import { oemContractsApi } from "../lib/oemContracts";
import { useInFlight } from "../lib/useInFlight";
import ContractWorkspace from "./contracts/ContractWorkspace";

/**
 * Service contracts in the manufacturer's portal (ADR-0021).
 *
 * A manufacturer drafts a contract for machines it has installed at one
 * customer, proposes it, and from then on works the monthly statements with
 * that customer: compute, accept, dispute. Nothing is measured until the
 * FACTORY accepts, and the factory's acceptance is what grants the downtime
 * sharing statements are built from.
 *
 * What AMP does NOT do is said where the contract is written: it computes the
 * credit from the terms and never invoices, charges or moves money.
 *
 * Capabilities come from /oem/me and only hide controls; the server decides.
 */

const CONTRACT_TYPES = ["AMC", "WARRANTY", "UPTIME_CLAUSE"];

/**
 * A contract draft: a new one, or `draft` edited in place before it is proposed.
 *
 * Editing matters because a reference is never reused (withdrawn contracts keep
 * theirs), so without it a typo in a saved draft's fee cost the manufacturer the
 * contract's reference. The server replaces the draft whole, and refuses once
 * the contract is proposed; after that the terms change only by amendment.
 */
function ContractDraftForm({ fleet, draft, onSaved, onClose }: {
  fleet: FleetMachine[];
  draft?: ContractDetail;
  onSaved: () => void;
  onClose: () => void;
}) {
  const { run, busy } = useInFlight();
  const draftTerms = draft?.versions.find((v) => v.version === 1)?.terms;
  const covered = useMemo(() => draftTerms?.covered_installations ?? [], [draftTerms]);
  const customers = useMemo(
    () => Array.from(new Set([
      ...fleet.map((m) => m.customer).filter((c): c is string => Boolean(c)),
      ...(draft ? [draft.factory_tenant_code] : []),
    ])).sort(),
    [fleet, draft],
  );
  const [ref, setRef] = useState(draft?.contract_ref ?? "");
  const [title, setTitle] = useState(draft?.title ?? "Annual maintenance contract");
  const [type, setType] = useState(draft?.contract_type ?? "AMC");
  const [customer, setCustomer] = useState(draft?.factory_tenant_code ?? "");
  const [startMonth, setStartMonth] = useState(
    () => (draft && draftTerms ? startMonthOf(draft.starts_at, draftTerms.timezone) : ""));
  const [chosen, setChosen] = useState<number[]>(() => covered.map((c) => c.installation_id));
  const [termsText, setTermsText] = useState(() => {
    const { covered_installations: _omit, ...rest } = draftTerms ?? termsTemplate([]);
    void _omit;
    return JSON.stringify(rest, null, 2);
  });
  const [error, setError] = useState("");
  const [created, setCreated] = useState("");

  // The customer's machines on the loaded fleet page, plus any the draft already
  // covers there that the page left out: an unrelated edit must not silently drop
  // a covered machine from the terms.
  const atCustomer = useMemo(() => {
    const here = fleet.filter((m) => m.customer === customer)
      .map((m) => ({ installation_id: m.installation_id, serial_number: m.serial_number }));
    const listed = new Set(here.map((m) => m.installation_id));
    const kept = customer === draft?.factory_tenant_code
      ? covered.filter((c) => !listed.has(c.installation_id)) : [];
    return [...here, ...kept];
  }, [fleet, customer, draft, covered]);

  function toggle(id: number) {
    setChosen((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setCreated("");
    let rest: Omit<Terms, "covered_installations">;
    try {
      rest = JSON.parse(termsText);
    } catch {
      setError("The terms are not valid JSON.");
      return;
    }
    const installations = atCustomer.filter((m) => chosen.includes(m.installation_id));
    const body = {
      contract_ref: ref.trim(), title: title.trim(), contract_type: type,
      factory_tenant_code: customer, start_month: startMonth,
      terms: { ...rest, covered_installations: installations } as Terms,
    };
    void run("save", async () => {
      try {
        if (draft) {
          await oemContractsApi.editDraft(draft.id, body);
        } else {
          const detail = await oemContractsApi.create(body);
          setCreated(`Draft ${detail.contract_ref} created. Review it below and propose it to ${customer}.`);
        }
        onSaved();
      } catch (err) {
        setError(contractError(err).message);
      }
    });
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-xl border border-slate-800 bg-slate-900/60 p-4 text-xs">
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="flex flex-col gap-1">Reference
          <input value={ref} onChange={(e) => setRef(e.target.value)} required
                 className="rounded bg-slate-800 px-2 py-1" />
        </label>
        <label className="flex flex-col gap-1">Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required
                 className="rounded bg-slate-800 px-2 py-1" />
        </label>
        <label className="flex flex-col gap-1">Type
          <select value={type} onChange={(e) => setType(e.target.value)}
                  className="rounded bg-slate-800 px-2 py-1">
            {CONTRACT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">Customer
          <select value={customer} onChange={(e) => { setCustomer(e.target.value); setChosen([]); }}
                  required className="rounded bg-slate-800 px-2 py-1">
            <option value="">choose a customer</option>
            {customers.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">First month (YYYY-MM)
          <input value={startMonth} onChange={(e) => setStartMonth(e.target.value)} required
                 placeholder="2026-10" className="rounded bg-slate-800 px-2 py-1 font-mono" />
        </label>
      </div>
      {customer && (
        <fieldset>
          <legend className="text-slate-400">Covered machines at {customer}</legend>
          {atCustomer.length === 0 && <p className="text-slate-500">No installations there.</p>}
          {atCustomer.map((m) => (
            <label key={m.installation_id} className="mr-4 inline-flex items-center gap-1">
              <input type="checkbox" checked={chosen.includes(m.installation_id)}
                     onChange={() => toggle(m.installation_id)} />
              <span className="font-mono">{m.serial_number}</span>
            </label>
          ))}
        </fieldset>
      )}
      <label className="flex flex-col gap-1">
        Terms: fee, SLA, credit tiers, trusted sources and how downtime is attributed
        <textarea aria-label="Contract terms" value={termsText} onChange={(e) => setTermsText(e.target.value)}
                  rows={14} className="rounded bg-slate-950 p-2 font-mono" />
      </label>
      <p className="text-slate-500">
        The terms are checked field by field when you save. Only MQTT is trusted by default:
        every status source is provisioned by the factory and none is authenticated as yours.
        AMP computes the credit from these terms; it never invoices or moves money.
      </p>
      <div className="flex gap-2">
        <button type="submit" disabled={busy("save")}
                className="rounded-lg bg-blue-600 px-3 py-1.5 font-medium text-white disabled:opacity-40">
          {draft ? "Save changes" : "Save draft"}
        </button>
        <button type="button" onClick={onClose}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-300">Close</button>
      </div>
      {created && <p className="text-emerald-400">{created}</p>}
      {error && <p role="alert" className="text-red-400">{error}</p>}
    </form>
  );
}

function NewContract({ fleet, onCreated }: { fleet: FleetMachine[]; onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}
              className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-medium text-white">
        Draft a service contract
      </button>
    );
  }
  return <ContractDraftForm fleet={fleet} onSaved={onCreated} onClose={() => setOpen(false)} />;
}

function OfferActions({ detail, canManage, canSign, fleet, reload }: {
  detail: ContractDetail; canManage: boolean; canSign: boolean; fleet: FleetMachine[];
  reload: () => void;
}) {
  const { run, busy } = useInFlight();
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  if (!canSign && !canManage) return null;
  const v1 = detail.versions.find((v) => v.version === 1);
  // Editing is managing (the server's MANAGE capability); proposing and withdrawing
  // are signing. A draft being edited is not offered for proposal until it is saved.
  const editable = canManage && detail.status === "draft" && Boolean(v1);
  async function act(key: string, fn: () => Promise<unknown>) {
    await run(key, async () => {
      setError("");
      try {
        await fn();
      } catch (e) {
        setError(contractError(e).message);
      }
      reload();
    });
  }
  return (
    <div className="space-y-2" data-testid="offer-actions">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {editable && (
          <button type="button" aria-expanded={editing} onClick={() => setEditing((on) => !on)}
                  className="rounded border border-slate-700 px-2 py-1 text-slate-300">
            Edit draft
          </button>
        )}
        {canSign && detail.status === "draft" && v1 && !editing && (
          <button type="button" disabled={busy("propose")}
                  onClick={() => act("propose", () => oemContractsApi.propose(detail.id, v1.terms_hash))}
                  className="rounded bg-blue-600 px-2 py-1 text-white">
            Propose to {detail.factory_tenant_code}
          </button>
        )}
        {canSign && (
          <button type="button" disabled={busy("withdraw")}
                  onClick={() => act("withdraw", () => oemContractsApi.withdraw(detail.id))}
                  className="rounded border border-slate-700 px-2 py-1 text-slate-300">
            Withdraw
          </button>
        )}
        {error && <span role="alert" className="text-red-400">{error}</span>}
      </div>
      {editable && editing && (
        <ContractDraftForm fleet={fleet} draft={detail}
                           onSaved={() => { setEditing(false); reload(); }}
                           onClose={() => setEditing(false)} />
      )}
    </div>
  );
}

export default function OemContracts({ capabilities, fleet }: {
  capabilities: string[];
  fleet: FleetMachine[];
}) {
  const [reloadToken, setReloadToken] = useState(0);

  // No read_contracts, no section and no request: the list endpoint would refuse
  // the call anyway, and a principal who cannot read contracts should not be shown
  // a panel that exists only to say so.
  if (!capabilities.includes("read_contracts")) return null;
  const canManage = capabilities.includes("manage_contracts");
  const canSign = capabilities.includes("sign_contracts");
  return (
    <section className="mt-8 space-y-4" data-testid="oem-contracts">
      <div>
        <h2 className="text-lg font-semibold text-white">Service contracts</h2>
        <p className="text-slate-500 text-xs mt-1 max-w-3xl">
          Monthly downtime attribution for the contracts you sign with customers. Every covered
          minute is Available, OEM, Factory, Disputed or No data, from the machine&apos;s status
          feed and the factory&apos;s own downtime reasons; both parties accept the exact statement.
          AMP computes credits and never moves money.
        </p>
      </div>
      {canManage && <NewContract fleet={fleet} onCreated={() => setReloadToken((n) => n + 1)} />}
      <ContractWorkspace
        api={oemContractsApi}
        canManage={canManage}
        canSign={canSign}
        reloadToken={reloadToken}
        renderOfferActions={(detail, reload) => (
          <OfferActions detail={detail} canManage={canManage} canSign={canSign} fleet={fleet}
                        reload={reload} />
        )}
      />
    </section>
  );
}
