"""Goal-driven coordination model — the replacement for the 26-state enum.

Instead of routing on a discrete `current_state`, we describe the world as the
sub-conditions of the coordination GOAL and let the agent reason about the
biggest gap, in a fixed party priority order.

GOAL: schedule a tasting-room visit by coordinating up to three parties —
  - CECIL / Winefornia (our side; the winemaker)
  - CUSTOMER (the guest who wants to visit; stored in the reservation's client_* fields)
  - JOSH (the facility coordinator at The Caves at Soda Canyon)
…then invoice, take payment, and confirm.

Two case types (from the Squarespace form / experience_type):
  - PRODUCTION_TOUR: "production tour + tasting WITH the winemaker" — Cecil
    PARTICIPATES, so the slot must align ALL THREE parties.
  - STANDARD: a normal tasting — Cecil does NOT participate; she only APPROVES.
    Scheduling is between two parties (Josh + customer), gated by Cecil's approval.

Status-check priority (both cases): 1) Cecil  2) Customer  3) Josh.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Case types ───────────────────────────────────────────────────────────────
PRODUCTION_TOUR = "production_tour"   # winemaker participates → 3-party schedule
STANDARD = "standard"                 # Cecil approves only → Josh+customer schedule

# ── Condition vocabularies ───────────────────────────────────────────────────
# Cecil: "ok" means available-for-the-slot (PRODUCTION_TOUR) OR approved (STANDARD).
UNKNOWN, OK, BLOCKED = "unknown", "ok", "blocked"
# Josh availability.
J_UNKNOWN, J_CONFIRMED, J_UNAVAILABLE = "unknown", "confirmed", "unavailable"
# Customer commitment.
NONE, OFFERED, ACCEPTED, DECLINED = "none", "offered", "accepted", "declined"
# Money / closeout.
NOT_SENT, SENT, PAID = "not_sent", "sent", "paid"


# The Squarespace "production tour + tasting with the winemaker" option is what
# makes a case 3-party. The selection arrives in the form EMAIL body (HTML), which
# the legacy pipeline did not persist to a structured field — so classify across
# every text field we have, keyword-robust, rather than trusting experience_type.
# Squarespace dropdown labels (exact):
#   standard        → "Tasting ($85 per person)"
#   production_tour → "Production Tour and Tasting with Winemaker ($110 per person)"
_TOUR_KEYWORDS = ("production tour", "tour and tasting with winemaker", "winemaker", "$110 per person")


def classify_case_type(reservation: dict, *, source_text: str = "") -> str:
    """PRODUCTION_TOUR if the winemaker tour was requested, else STANDARD.

    Checks experience_type, notes, and any inbound form text (source_text) — the
    intake step should pass the form email body here so the case type is captured
    even though experience_type is often empty.
    """
    r = reservation or {}
    haystack = " ".join([
        str(r.get("experience_type") or ""),
        str(r.get("notes") or ""),
        source_text or "",
    ]).lower()
    if any(kw in haystack for kw in _TOUR_KEYWORDS):
        return PRODUCTION_TOUR
    return STANDARD


@dataclass
class GoalState:
    """Derived view of how close a reservation is to a coordinated, confirmed visit."""

    case_type: str = STANDARD
    cecil_status: str = UNKNOWN        # unknown | ok | blocked  (approval or availability)
    customer_commitment: str = NONE    # none | offered | accepted | declined
    josh_availability: str = J_UNKNOWN  # unknown | confirmed | unavailable
    invoice: str = NOT_SENT            # not_sent | sent | paid
    confirmation: str = NOT_SENT       # not_sent | sent

    @property
    def parties(self) -> list[str]:
        """Who must be coordinated for this case (priority order)."""
        return ["cecil", "customer", "josh"] if self.case_type == PRODUCTION_TOUR \
            else ["cecil", "customer", "josh"]  # Cecil = approval-only in STANDARD, still checked first

    def is_goal_met(self) -> bool:
        return (
            self.cecil_status == OK
            and self.customer_commitment == ACCEPTED
            and self.josh_availability == J_CONFIRMED
            and self.invoice == PAID
            and self.confirmation == SENT
        )

    def gaps(self) -> list[str]:
        """Open gaps toward the goal, ORDERED by party priority: Cecil → Customer → Josh.

        The LLM agent uses this as the priority hint; it still applies coordination
        sense (e.g. don't offer the customer a slot before availability is known).
        """
        g: list[str] = []
        # Hard blocks first — a declined/unavailable party needs human attention.
        if self.cecil_status == BLOCKED or self.josh_availability == J_UNAVAILABLE \
                or self.customer_commitment == DECLINED:
            g.append("blocked_needs_alternatives_or_escalation")

        # 1) CECIL — approval (STANDARD) or availability (PRODUCTION_TOUR).
        if self.cecil_status == UNKNOWN:
            g.append("need_cecil_availability" if self.case_type == PRODUCTION_TOUR
                     else "need_cecil_approval")
        # Gather Josh's availability before offering the customer a slot — you
        # can't offer a time that isn't confirmed available.
        if self.josh_availability == J_UNKNOWN:
            g.append("need_josh_availability")
        # CUSTOMER — offer a slot only once it's actually available (Cecil ok AND
        # Josh confirmed). Then chase acceptance.
        if (self.cecil_status == OK and self.josh_availability == J_CONFIRMED
                and self.customer_commitment in (NONE, DECLINED)):
            g.append("offer_slot_to_customer")

        # Closeout (only once the visit is agreed).
        if (self.cecil_status == OK and self.customer_commitment == ACCEPTED
                and self.josh_availability == J_CONFIRMED):
            if self.invoice == NOT_SENT:
                g.append("send_invoice")
            elif self.invoice == SENT:
                g.append("await_or_check_payment")
            elif self.invoice == PAID and self.confirmation == NOT_SENT:
                g.append("send_final_confirmation")
        return g


def derive_goal_state(reservation: dict, claims: list[dict]) -> GoalState:
    """Map a reservation row + its availability claims onto the goal conditions."""
    state = (reservation or {}).get("current_state", "") or ""
    payment = (reservation or {}).get("payment_status", "") or NOT_SENT
    booking = (reservation or {}).get("booking_status", "") or ""
    case_type = classify_case_type(reservation)
    gs = GoalState(case_type=case_type)

    def _claim_status(actors: set[str]) -> str | None:
        rel = [c for c in claims if (c.get("actor") in actors)]
        if any((c.get("claim_status") or "").lower() in ("unavailable", "declined") for c in rel):
            return "blocked"
        if any((c.get("claim_status") or "").lower() in ("available", "confirmed", "approved") for c in rel):
            return "ok"
        return None

    # CECIL (our side / winemaker / internal).
    cecil_claim = _claim_status({"cecil", "internal_staff", "internal", "winefornia"})
    if cecil_claim == "blocked" or state in ("INTERNAL_UNAVAILABLE", "NO_COMMON_SLOT"):
        gs.cecil_status = BLOCKED
    elif cecil_claim == "ok" or state in (
        "INTERNAL_AVAILABLE", "READY_TO_OFFER_CLIENT", "SLOT_OFFERED_TO_CLIENT",
        "CLIENT_ACCEPTED_SLOT", "TENTATIVELY_BOOKED", "INVOICE_SENT",
        "WAITING_FOR_PAYMENT", "PAYMENT_RECEIVED", "FINAL_CONFIRMED",
    ):
        gs.cecil_status = OK
    else:
        gs.cecil_status = UNKNOWN

    # JOSH (facility).
    josh_claim = _claim_status({"josh", "facility"})
    if josh_claim == "blocked" or state in ("JOSH_UNAVAILABLE", "NO_COMMON_SLOT"):
        gs.josh_availability = J_UNAVAILABLE
    elif josh_claim == "ok" or state in (
        "FACILITY_AVAILABLE", "READY_TO_OFFER_CLIENT", "SLOT_OFFERED_TO_CLIENT",
        "CLIENT_ACCEPTED_SLOT", "TENTATIVELY_BOOKED", "INVOICE_SENT",
        "WAITING_FOR_PAYMENT", "PAYMENT_RECEIVED", "FINAL_CONFIRMED",
    ):
        gs.josh_availability = J_CONFIRMED
    else:
        gs.josh_availability = J_UNKNOWN

    # CUSTOMER (the visiting guest — reservation client_* fields).
    if state in ("CLIENT_ACCEPTED_SLOT", "TENTATIVELY_BOOKED", "INVOICE_SENT",
                 "WAITING_FOR_PAYMENT", "PAYMENT_RECEIVED", "FINAL_CONFIRMED"):
        gs.customer_commitment = ACCEPTED
    elif state == "SLOT_OFFERED_TO_CLIENT":
        gs.customer_commitment = OFFERED
    elif state == "CLIENT_REQUESTED_ALTERNATIVE":
        gs.customer_commitment = DECLINED
    else:
        gs.customer_commitment = NONE

    # Money / confirmation.
    if payment in (PAID, "paid") or state in ("PAYMENT_RECEIVED", "FINAL_CONFIRMED"):
        gs.invoice = PAID
    elif payment in (SENT, "sent") or state in ("INVOICE_SENT", "WAITING_FOR_PAYMENT"):
        gs.invoice = SENT
    else:
        gs.invoice = NOT_SENT
    gs.confirmation = SENT if (state == "FINAL_CONFIRMED" or booking in ("confirmed", "final_confirmed")) else NOT_SENT
    return gs
