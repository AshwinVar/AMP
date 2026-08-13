import { API_URL, getToken, getAuthHeaders } from "./api";

/**
 * The OEM portal's client (ADR-0017).
 *
 * A manufacturer is a DIFFERENT PERSONA from a factory user, not a factory user
 * with extra tabs: different login, different branding, different questions.
 * So the portal is its own route rather than another `activeView` inside the
 * 2,900-line factory dashboard, and this module is the only place that talks to
 * `/oem`.
 *
 * `isOemSession()` reads the `principal` claim rather than the role, because a
 * role string can coincide — the backend already refuses an OEM token on factory
 * routes and vice versa, so this is purely about showing the right screen, never
 * about deciding what anybody is allowed to see. That decision is the server's.
 */

export type FleetMachine = {
  installation_id: number;
  serial_number: string;
  model_code: string | null;
  model_name: string | null;
  customer: string | null;
  site: string;
  lifecycle_status: string;
  installed_at: string | null;
  commissioned_at: string | null;
  warranty_start: string | null;
  warranty_end: string | null;
  /** null means NOT SHARED — see `shared`. It never means zero. */
  operating_hours: number | null;
  last_seen_at: string | null;
  machine_status: string | null;
  utilization: number | null;
  shared: string[];
  not_shared?: string[];
};

export type OemIdentity = {
  oem_code: string;
  username: string;
  role: string;
  capabilities: string[];
  branding: {
    name: string;
    color: string | null;
    logo_url: string | null;
    support_email: string | null;
    support_phone: string | null;
  };
};

export type ServiceRecommendation = {
  kind: string;
  severity: "high" | "medium" | "low";
  machine: string;
  reason: string;
  evidence: string;
  action: string;
  /** Present ONLY on projections. null means "this is arithmetic". */
  confidence: number | null;
  at: string;
};

export function isOemSession(): boolean {
  const token = getToken();
  if (!token) return false;
  try {
    return JSON.parse(atob(token.split(".")[1])).principal === "oem";
  } catch {
    return false;
  }
}

/**
 * A refusal from `/oem`, with the STATUS kept.
 *
 * The status is the difference between "you are signed in as a factory user"
 * (401 "Not an OEM session") and "that machine is not yours" (404), and the
 * portal reacts differently to each. Throwing a bare Error would force the page
 * to pattern-match on English, which breaks the first time a message is
 * reworded.
 */
export class OemRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "OemRequestError";
    this.status = status;
  }
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    // The backend's own reason, not a generic one. "Your session is not an OEM
    // session" and "that machine is not yours" need different reactions, and a
    // swallowed message makes both look like an outage.
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* a non-JSON error body is still an error; keep the status */
    }
    throw new OemRequestError(response.status, detail);
  }
  return response.json();
}

export const fetchOemIdentity = () => get<OemIdentity>("/oem/me");

export const fetchFleet = (customer?: string, limit = 100, offset = 0) =>
  get<{ total: number; limit: number; offset: number; machines: FleetMachine[] }>(
    `/oem/fleet?limit=${limit}&offset=${offset}` +
      (customer ? `&customer=${encodeURIComponent(customer)}` : ""),
  );

export const fetchCustomers = () =>
  get<{
    customers: Array<{
      customer: string;
      site: string;
      machines: number;
      active: number;
      shared: string[];
    }>;
  }>("/oem/customers");

export const fetchServiceQueue = () =>
  get<{
    total: number;
    by_severity: Record<string, number>;
    recommendations: ServiceRecommendation[];
  }>("/oem/service");

export const fetchMachine = (id: number) => get<FleetMachine>(`/oem/machines/${id}`);

/** This manufacturer's catalogue — what a new machine can be registered as. */
export const fetchModels = () =>
  get<Array<{ id: number; model_code: string; name: string }>>("/oem/models");

// ── Registering machines and offering them (ADR-0019) ────────────────

export type OemClaim = {
  id: number;
  serial_number: string | null;
  model_code: string | null;
  status: "Pending" | "Claimed" | "Revoked" | "Expired";
  code_hint: string;
  intended_customer: string | null;
  expires_at: string | null;
  created_at: string | null;
  created_by: string | null;
  claimed_at: string | null;
  /** Which customer accepted it — the OEM's own record, not a peek at anyone. */
  claimed_by_customer: string | null;
};

/**
 * The response to CREATING a claim, which is the only time the raw code exists.
 *
 * `claim_code` is deliberately absent from `OemClaim`: AMP stores a hash, so a
 * listing can never show it again. A UI that expected to re-read it later would
 * be designing against a guarantee the backend makes on purpose.
 */
export type OemClaimCreated = OemClaim & {
  claim_code: string;
  claim_url: string;
  note: string;
};

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const parsed = await response.json();
      if (parsed?.detail) detail = String(parsed.detail);
    } catch {
      /* a non-JSON error body is still an error; keep the status */
    }
    throw new OemRequestError(response.status, detail);
  }
  return response.json();
}

export const registerMachine = (body: {
  serial_number: string;
  model_id: number;
  firmware_version?: string | null;
  warranty_start?: string | null;
  warranty_end?: string | null;
}) =>
  post<{ installation_id: number; serial_number: string; model_code: string }>(
    "/oem/machines",
    body,
  );

export const createClaim = (installationId: number, expiresInDays?: number) =>
  post<OemClaimCreated>(`/oem/machines/${installationId}/claim`, {
    expires_in_days: expiresInDays ?? null,
  });

export const revokeClaim = (claimId: number) =>
  post<OemClaim>(`/oem/claims/${claimId}/revoke`, {});

export const fetchClaims = () =>
  get<{ total: number; claims: OemClaim[] }>("/oem/claims");

export const fetchOemNotifications = () =>
  get<{
    notifications: Array<{
      id: number;
      type: string;
      title: string;
      message: string;
      created_at: string | null;
    }>;
  }>("/oem/notifications");

export const fetchMachineService = (id: number) =>
  get<{
    installation_id: number;
    serial_number: string;
    lifecycle_status: string;
    service: { state: string; reason: string; hours_remaining: number | null };
    warranty: { state: string; reason: string; days_remaining: number | null };
    commissioning: {
      ready: boolean;
      checks: Array<{ key: string; description: string; passed: boolean; detail: string }>;
    };
    telemetry_signals: string[];
    telemetry_profile_error: string | null;
    recommendations: ServiceRecommendation[];
  }>(`/oem/machines/${id}/service`);

/**
 * How a fleet field should read when the customer has not shared it.
 *
 * "—" and 0 are different facts, and conflating them is the whole reason this
 * helper exists: a service desk that reads "0 hours" for a machine whose owner
 * simply declined to say will book an engineer against a number nobody supplied.
 */
export function shareable(
  value: number | string | null,
  granted: boolean,
  suffix = "",
): string {
  if (!granted) return "not shared";
  if (value === null || value === undefined) return "no data";
  return `${value}${suffix}`;
}

/** Fleet headline counts. Derived from what the OEM can actually see. */
export function fleetSummary(machines: FleetMachine[]) {
  const now = Date.now();
  const isStale = (seen: string | null) =>
    seen ? now - new Date(seen).getTime() > 48 * 3600 * 1000 : null;

  let connected = 0;
  let offline = 0;
  let unknown = 0;
  for (const m of machines) {
    const stale = isStale(m.last_seen_at);
    // A machine whose owner has not shared health is NOT "offline" — it is
    // unknown, and counting it as offline would invent a fleet problem out of a
    // privacy setting.
    if (stale === null) unknown += 1;
    else if (stale) offline += 1;
    else connected += 1;
  }
  return {
    total: machines.length,
    connected,
    offline,
    unknown,
    active: machines.filter((m) => m.lifecycle_status === "Active").length,
    warrantyActive: machines.filter(
      (m) => m.warranty_end && new Date(m.warranty_end).getTime() > now,
    ).length,
  };
}
