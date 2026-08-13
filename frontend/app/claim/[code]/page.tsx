"use client";

import { useParams, useRouter } from "next/navigation";

import AddConnectedEquipment from "../../../components/AddConnectedEquipment";
import { getToken } from "../../../lib/api";
import { isOemSession } from "../../../lib/oem";

/**
 * Where the QR on the machine points (ADR-0019).
 *
 * The sticker encodes `/claim/AMP-XXXXX-XXXXX-XXXXX`, so somebody standing next
 * to a crate can scan it instead of typing twenty characters off a label with
 * oil on it. That is the whole benefit, and it is worth being precise about what
 * it is NOT:
 *
 *   OPENING THIS PAGE CLAIMS NOTHING. It fills the box. The lookup is still a
 *   press, the acceptance is still a separate press, and both are refused for
 *   anyone who is not an Admin of a factory. A link forwarded to the wrong
 *   person, or scanned by somebody walking past, attaches no equipment to
 *   anybody's workspace.
 *
 * The three states this page can be in are all real: nobody is signed in (the
 * common one — a phone camera opens a browser with no session), a manufacturer
 * is signed in (their portal cannot accept machines, and saying so is kinder
 * than a 403), or a factory user is signed in and can get on with it.
 */
export default function ClaimPage() {
  const router = useRouter();
  const params = useParams<{ code: string }>();
  const code = decodeURIComponent(
    Array.isArray(params?.code) ? params.code[0] : params?.code || "",
  );

  const signedIn = Boolean(getToken());
  const oem = signedIn && isOemSession();

  function signIn() {
    // Come back here afterwards. Without this the code is lost at the login
    // screen and the person is left retyping the label they just scanned.
    try {
      localStorage.setItem("afterLogin", `/claim/${encodeURIComponent(code)}`);
    } catch {
      /* a browser that refuses storage still gets a working login */
    }
    router.push("/login");
  }

  return (
    <main className="min-h-screen bg-slate-950 p-6">
      <div className="mx-auto max-w-3xl">
        <header className="mb-6">
          <h1 className="text-2xl font-bold text-white">Add this machine</h1>
          <p className="mt-1 text-sm text-slate-400">
            You scanned a claim code. Nothing has been added yet — check the
            machine below is the one in front of you, then decide what its
            manufacturer may see.
          </p>
          <p className="mt-2 font-mono text-sm text-slate-300">{code || "—"}</p>
        </header>

        {!signedIn && (
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
            <p className="text-slate-300">
              Sign in with your AMP account to add this machine to your
              workspace. You will come back to this page.
            </p>
            <button
              type="button"
              onClick={signIn}
              className="mt-4 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white"
            >
              Sign in
            </button>
          </div>
        )}

        {oem && (
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
            <p className="text-slate-300">
              You are signed in as a manufacturer. A machine is added by the
              factory that receives it, not by the company that built it — ask
              your customer&apos;s AMP administrator to scan this code.
            </p>
            <button
              type="button"
              onClick={() => router.push("/oem")}
              className="mt-4 rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300"
            >
              Back to your portal
            </button>
          </div>
        )}

        {signedIn && !oem && (
          <AddConnectedEquipment
            initialCode={code}
            onAdded={() => router.push("/dashboard")}
          />
        )}
      </div>
    </main>
  );
}
