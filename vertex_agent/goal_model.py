"""Goal-driven coordination model — the replacement for the 26-state enum.

Instead of routing on a discrete `current_state`, we describe the world as the
sub-conditions of the coordination GOAL and let the agent reason about the
biggest gap. This is a *modest* evolution of the existing `current_truth`
judgment, not a rewrite: the conditions are derived from the same reservation
fields and availability claims the current pipeline already produces.

GOAL: one slot where facility (Josh) + internal staff + client all agree,
      invoiced, paid, and confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Condition value vocabularies ─────────────────────────────────────────────
UNKNOWN, CONFIRMED, UNAVAILABLE = "unknown", "confirmed", "unavailable"
NONE, OFFERED, ACCEPTED, DECLINED = "none", "offered", "accepted", "declined"
NOT_SENT, SENT, PAID = "not_sent", "sent", "paid"


@dataclass
class GoalState:
    """Derived view of how close a reservation is to the coordination goal."""

    facility_availability: str = UNKNOWN   # unknown | confirmed | unavailable
    internal_availability: str = UNKNOWN   # unknown | confirmed | unavailable
    client_commitment: str = NONE          # none | offered | accepted | declined
    invoice: str = NOT_SENT                # not_sent | sent | paid
    confirmation: str = NOT_SENT           # not_sent | sent

    def is_goal_met(self) -> bool:
        return (
            self.facility_availability == CONFIRMED
            and self.internal_availability == CONFIRMED
            and self.client_commitment == ACCEPTED
            and self.invoice == PAID
            and self.confirmation == SENT
        )

    def gaps(self) -> list[str]:
        """Ordered list of what still blocks the goal — the agent closes the first."""
        g: list[str] = []
        if self.facility_availability == UNAVAILABLE or self.internal_availability == UNAVAILABLE:
            g.append("no_common_slot")  # needs alternatives / human attention
        if self.facility_availability == UNKNOWN:
            g.append("need_facility_availability")
        if self.internal_availability == UNKNOWN:
            g.append("need_internal_availability")
        if (self.facility_availability == CONFIRMED
                and self.internal_availability == CONFIRMED
                and self.client_commitment in (NONE, DECLINED)):
            g.append("offer_slot_to_client")
        if self.client_commitment == ACCEPTED and self.invoice == NOT_SENT:
            g.append("send_invoice")
        if self.invoice == SENT:
            g.append("await_or_check_payment")
        if self.invoice == PAID and self.confirmation == NOT_SENT:
            g.append("send_final_confirmation")
        return g


def derive_goal_state(reservation: dict, claims: list[dict]) -> GoalState:
    """Map a reservation row + its availability claims onto the goal conditions.

    Derived from existing fields (current_state, payment_status, booking_status)
    plus actor-tagged claims, so adopting this does not require any data changes.
    """
    state = (reservation or {}).get("current_state", "") or ""
    payment = (reservation or {}).get("payment_status", "") or NOT_SENT
    booking = (reservation or {}).get("booking_status", "") or ""

    gs = GoalState()

    # Facility / internal availability — prefer explicit claims, fall back to state.
    def _availability(actors: set[str], confirmed_states: set[str], unavailable_states: set[str]) -> str:
        relevant = [c for c in claims if (c.get("actor") in actors)]
        if any((c.get("claim_status") or "").lower() in ("unavailable", "declined") for c in relevant):
            return UNAVAILABLE
        if any((c.get("claim_status") or "").lower() in ("available", "confirmed") for c in relevant):
            return CONFIRMED
        if state in unavailable_states:
            return UNAVAILABLE
        if state in confirmed_states:
            return CONFIRMED
        return UNKNOWN

    gs.facility_availability = _availability(
        {"josh", "facility"},
        {"FACILITY_AVAILABLE", "READY_TO_OFFER_CLIENT", "SLOT_OFFERED_TO_CLIENT",
         "CLIENT_ACCEPTED_SLOT", "TENTATIVELY_BOOKED", "INVOICE_SENT",
         "WAITING_FOR_PAYMENT", "PAYMENT_RECEIVED", "FINAL_CONFIRMED"},
        {"JOSH_UNAVAILABLE", "NO_COMMON_SLOT"},
    )
    gs.internal_availability = _availability(
        {"internal_staff", "internal"},
        {"INTERNAL_AVAILABLE", "READY_TO_OFFER_CLIENT", "SLOT_OFFERED_TO_CLIENT",
         "CLIENT_ACCEPTED_SLOT", "TENTATIVELY_BOOKED", "INVOICE_SENT",
         "WAITING_FOR_PAYMENT", "PAYMENT_RECEIVED", "FINAL_CONFIRMED"},
        {"INTERNAL_UNAVAILABLE", "NO_COMMON_SLOT"},
    )

    # Client commitment.
    if state in ("CLIENT_ACCEPTED_SLOT", "TENTATIVELY_BOOKED", "INVOICE_SENT",
                 "WAITING_FOR_PAYMENT", "PAYMENT_RECEIVED", "FINAL_CONFIRMED"):
        gs.client_commitment = ACCEPTED
    elif state == "SLOT_OFFERED_TO_CLIENT":
        gs.client_commitment = OFFERED
    elif state in ("CLIENT_REQUESTED_ALTERNATIVE",):
        gs.client_commitment = DECLINED
    else:
        gs.client_commitment = NONE

    # Invoice / payment.
    if payment in (PAID, "paid") or state in ("PAYMENT_RECEIVED", "FINAL_CONFIRMED"):
        gs.invoice = PAID
    elif payment in (SENT, "sent") or state in ("INVOICE_SENT", "WAITING_FOR_PAYMENT"):
        gs.invoice = SENT
    else:
        gs.invoice = NOT_SENT

    # Final confirmation.
    gs.confirmation = SENT if (state == "FINAL_CONFIRMED" or booking in ("confirmed", "final_confirmed")) else NOT_SENT
    return gs
