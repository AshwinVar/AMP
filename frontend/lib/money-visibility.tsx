"use client";

import { createContext, useContext, type ReactNode } from "react";

/**
 * CAN THIS VIEWER SEE MONEY? — the one answer, for the whole dashboard.
 *
 * Money on the dashboard (every rupee figure: part prices, plant revenue, the
 * cost of losses, recovery upside, the scorecard's loss_cost KPI, SaaS MRR, …)
 * is for Admins. The backend now OMITS those fields for everyone else, so a
 * non-Admin's payload simply has no amount in it; this context is the matching
 * front-end gate, so a card never renders an empty hole, a stale zero, or a
 * broken "NaN" currency string where a figure used to be.
 *
 * ONE SOURCE, NO PROP-DRILLING. app/dashboard/page.tsx already derives
 * `isAdmin` from the login token (role === "Admin"). It wraps the whole
 * dashboard in <CanSeeMoneyProvider value={isAdmin}> and every money-bearing
 * card reads useCanSeeMoney() — not a boolean threaded through dozens of
 * components. isAdmin stays a prop only where it gates WRITES (who may edit a
 * price or a rate); "may I SEE money" is this hook, and only this hook.
 *
 * DEFAULT true. A card rendered with no provider is only ever a unit test, and
 * defaulting to visible keeps the many existing card tests that assert a figure
 * passing unchanged. In the product these cards mount only under the dashboard,
 * which always supplies the real value, and the server omitting the amount is
 * the authoritative gate — this is defence in depth and the UX that goes with
 * it. A test that wants the hidden branch wraps the card in the provider with
 * value={false}.
 */
const CanSeeMoneyContext = createContext<boolean>(true);

export function CanSeeMoneyProvider({
  value,
  children,
}: {
  value: boolean;
  children: ReactNode;
}) {
  return (
    <CanSeeMoneyContext.Provider value={value}>{children}</CanSeeMoneyContext.Provider>
  );
}

/** True only when the current viewer may see money (Admin). */
export function useCanSeeMoney(): boolean {
  return useContext(CanSeeMoneyContext);
}
