# Tasting Room Agent Real Case - Mira Park

Created: 2026-05-29
Source account: lisa@innovatuswine.com
Case type: real completed tasting room booking

## Case Summary

This is a real end-to-end reservation case for Mira Park. It covers the full path the tasting room agent is designed to handle:

1. Squarespace tasting request arrives.
2. Audrey/INNOVATUS replies that the requested date is full.
3. Client asks for alternate dates.
4. Audrey checks availability with Josh at The Caves.
5. Josh provides open slots.
6. Audrey offers a slot to the client.
7. Client asks for another option.
8. Audrey offers June 7 at 2:30 PM.
9. Client accepts.
10. Audrey asks Josh to book the slot.
11. Josh confirms bookings.
12. Audrey sends tentative booking and Square invoice link.
13. Square reports invoice created.
14. Square reports invoice paid.
15. Audrey sends final confirmation.

Final booking:
- Client: Mira Park
- Email: mirasopa@gmail.com
- Party size: 2
- Experience: Production Tour and Tasting with Winemaker
- Final date/time: Sunday, June 7, 2026, 2:30-4:00 PM PST
- Venue: The Caves at Soda Canyon, Tasting Room
- Square invoice: #202440
- Amount: $237.05
- Final status: confirmed

## Gmail Threads and Messages

Client-facing thread:
- Thread ID: `19df02adfb641256`
- Initial subject: `Form Submission - Wine tasting Booking`
- Main participants: Mira Park, INNOVATUS/contact@innovatuswine.com

Josh availability thread:
- Thread ID: `19df9e62e583c5cc`
- Subject: `Re: 5/19 Availability`
- Main participants: INNOVATUS/contact@innovatuswine.com, Josh Uran/josh@thecavesatsodacanyon.com

Josh grouped booking confirmation thread:
- Thread ID: `19e0406b9f35a7f1`
- Subject: `Confirming Bookings for Winefornia`
- Main participants: INNOVATUS/contact@innovatuswine.com, Josh Uran/josh@thecavesatsodacanyon.com

Square invoice notification threads:
- Invoice created message/thread: `19e04c04e220f935`
- Invoice paid message/thread: `19e04cba91d71bb4`

Final confirmation thread:
- Thread ID: `19e1e362d0f43630`
- Subject: `Your Reservation at INNOVATUS is Confirmed - June 7`

## Timeline With Agent Interpretation

### 1. Initial Squarespace Request

Message ID: `19df0672d1adc9e9`
Date: 2026-05-04 00:33 UTC
From: Squarespace <form-submission@squarespace.info>
To: contact@innovatuswine.com
Subject: `Form Submission - Wine tasting Booking`

Real facts:
- Name: Mira Park
- Email: mirasopa@gmail.com
- Phone: (202) 734-8246
- Requested date: May 9, 2026
- Requested time: 2:30 PM
- Guests: 2
- Experience: Production Tour and Tasting with Winemaker ($110 per person)

Agent classification:
- `squarespace_form`

Agent state/action:
- Creates or matches reservation case.
- Stores a client `requested_slot` claim for 2026-05-09 at 14:30.
- State becomes `WAITING_FOR_JOSH`.
- Recommended action becomes `ask_internal_availability`.

Important implementation note:
- The actual current code first asks for internal availability after a form submission, even though this real historical case had Audrey manually reply that May 9 was fully booked.

### 2. Audrey Says May 9 Is Full

Message ID: `19df94a68b074bea`
Date: Tue, 2026-05-05 10:58:16 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: mirasopa@gmail.com
Subject: `Re: Form Submission - Wine tasting Booking`

Real email summary:
- Audrey thanks Mira.
- Says May 9 is fully booked.
- Asks for any other dates that work.

Agent classification:
- `staff_unavailable_reply`

Agent state/action:
- State becomes `WAITING_FOR_CLIENT_REPLY`.
- No outbound action needed.

### 3. Client Asks About June 6

Message ID: `19df9873ff793418`
Date: Tue, 2026-05-05 12:04:29 -0700
From: Mira Park <mirasopa@gmail.com>
To: INNOVATUS <contact@innovatuswine.com>
Subject: `Re: Form Submission - Wine tasting Booking`

Real email summary:
- Mira asks whether there is availability on 6/6.

Agent classification:
- `client_alternative_request`

Agent facts:
- Candidate date: 2026-06-06
- Client remains Mira Park through thread matching.

Agent state/action:
- State becomes `CLIENT_REQUESTED_ALTERNATIVE`.
- Recommended action becomes `ask_josh_availability`.

### 4. Audrey Asks Josh About June 6 and June 7

Message ID: `19df9f4685ae7dae`
Date: Tue, 2026-05-05 14:03:57 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: Josh Uran <josh@thecavesatsodacanyon.com>
Subject: `Re: 5/19 Availability`

Real email summary:
- Audrey asks Josh for availability for a party of 2 on 6/6 and 6/7.

Agent classification:
- `facility_availability_request`

Agent state/action:
- Stores an internal staff claim that availability was requested.
- State becomes `WAITING_FOR_JOSH`.
- No new action is needed until Josh replies.

Important implementation note:
- This email lives in a separate Josh thread, not the client thread. The agent must link it by contextual date/party matching or explicit reservation IDs in future generated drafts.

### 5. Josh Sends Open Slots

Message ID: `19df9f9e60702141`
Date: Tue, 2026-05-05 21:09:52 UTC
From: Josh Uran <josh@thecavesatsodacanyon.com>
To: INNOVATUS <contact@innovatuswine.com>
Subject: `Re: 5/19 Availability`

Real email summary:
- Josh says:
  - 6/6: 10:00 and 2:30 open
  - 6/7: 10:00, 12:30, and 2:30 open

Agent classification:
- `josh_availability_reply`

Agent claims:
- Facility availability available for 2026-06-06 10:00
- Facility availability available for 2026-06-06 14:30
- Facility availability available for 2026-06-07 10:00
- Facility availability available for 2026-06-07 12:30
- Facility availability available for 2026-06-07 14:30

Agent state/action:
- If internal availability has also been marked available, state becomes `READY_TO_OFFER_CLIENT` and action becomes `offer_client_slot`.
- If internal availability is missing, state becomes `NEEDS_INTERNAL_CHECK` and action becomes `ask_internal_availability`.

### 6. Audrey Offers June 6 at 2:30

Message ID: `19dfa40ff307cdf9`
Date: Tue, 2026-05-05 15:27:36 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: Mira Park <mirasopa@gmail.com>
Subject: `Re: Form Submission - Wine tasting Booking`

Real email summary:
- Audrey says she should have availability on 6/6/26 for the 2:30 PM slot.
- Asks whether Mira would like to book it.

Agent classification:
- `staff_slot_offer`

Agent state/action:
- Stores internal availability for 2026-06-06 14:30.
- Applies active slot to 2026-06-06 14:30.
- State becomes `SLOT_OFFERED_TO_CLIENT`.
- No further action until client replies.

### 7. Client Requests June 7 AM or 2:30

Message ID: `19dfadd1a31a0c6c`
Date: Tue, 2026-05-05 18:17:53 -0700
From: Mira Park <mirasopa@gmail.com>
To: INNOVATUS <contact@innovatuswine.com>
Subject: `Re: Form Submission - Wine tasting Booking`

Real email summary:
- Mira says she has a conflict.
- Asks about 6/7 in the morning or 2:30.

Agent classification:
- `client_alternative_request`

Agent state/action:
- State becomes `CLIENT_REQUESTED_ALTERNATIVE`.
- Candidate slots include 2026-06-07 morning and/or 14:30.
- Recommended action becomes `ask_josh_availability`.

Implementation nuance:
- Because Josh had already provided June 7 availability in another thread, the ideal agent behavior is to reuse that source-backed availability claim and offer the best slot instead of asking Josh again.

### 8. Audrey Offers June 7 at 2:30

Message ID: `19dff3368c7accbc`
Date: Wed, 2026-05-06 14:30:52 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: Mira Park <mirasopa@gmail.com>
Subject: `Re: Form Submission - Wine tasting Booking`

Real email summary:
- Audrey says 6/7 at 2:30 PM is open for a two-person Tour and Tasting with the winemaker.
- Asks Mira whether she would like to book.

Agent classification:
- `staff_slot_offer`

Agent state/action:
- Active slot becomes 2026-06-07 14:30.
- State becomes `SLOT_OFFERED_TO_CLIENT`.
- No further action until client replies.

### 9. Client Accepts June 7 at 2:30

Message ID: `19dff89610d3bf27`
Date: Wed, 2026-05-06 16:04:08 -0700
From: Mira Park <mirasopa@gmail.com>
To: INNOVATUS <contact@innovatuswine.com>
Subject: `Re: Form Submission - Wine tasting Booking`

Real email summary:
- Mira says: yes, please reserve that time for two people.

Agent classification:
- `client_acceptance`

Agent state/action:
- State becomes `CLIENT_ACCEPTED_SLOT`.
- Recommended action becomes `send_tentative_invoice`.

Important implementation note:
- Current code moves client acceptance directly toward `send_tentative_invoice`.
- Operationally, this real case still includes a separate Josh booking request and Josh/group confirmation before the invoice step. That gap is worth tightening in the agent flow.

### 10. Audrey Asks Josh To Book June 7 at 2:30

Message ID: `19dfe66e1a701059`
Date: Wed, 2026-05-06 10:47:28 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: Josh Uran <josh@thecavesatsodacanyon.com>
Subject: `Re: 5/19 Availability`

Real email summary:
- Audrey asks Josh: can I also book 6/7 at 2:30 for a party of 2?

Agent classification:
- `facility_booking_request`

Agent state/action:
- Stores internal staff `facility_booking_request`.
- Applies active slot 2026-06-07 14:30.
- Booking status becomes `facility_booking_requested`.
- State becomes `TENTATIVELY_BOOKED`.
- Recommended action becomes `send_tentative_invoice`.

### 11. Audrey Sends Grouped Booking Confirmation Request

Message ID: `19e040dcafc2705a`
Date: Thu, 2026-05-07 13:07:52 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: Josh Uran <josh@thecavesatsodacanyon.com>
Subject: `Confirming Bookings for Winefornia`

Real email summary:
- Audrey asks Josh to confirm multiple bookings:
  - 8/1 for 5 people at 2:30 PM
  - 6/7 for 2 people at 2:30 PM
  - 5/10 for 6 people at 12:30 PM

Agent classification:
- `josh_booking_confirmation` or booking-context related, depending on sender and wording.

Agent matching challenge:
- This message contains multiple bookings in one email. The agent must associate the 6/7, 2-person, 2:30 PM line with Mira's reservation while not corrupting other reservation cases.

### 12. Josh Confirms Grouped Bookings

Message ID: `19e040fe53faf6c9`
Date: Thu, 2026-05-07 20:10:06 UTC
From: Josh Uran <josh@thecavesatsodacanyon.com>
To: INNOVATUS <contact@innovatuswine.com>
Subject: `Re: Confirming Bookings for Winefornia`

Real email summary:
- Josh replies that these are confirmed.

Agent classification:
- `josh_booking_confirmation`

Agent state/action:
- Stores facility booking confirmation.
- Booking status becomes `facility_confirmed`.
- State becomes `TENTATIVELY_BOOKED`.
- Recommended action becomes `send_tentative_invoice`.

### 13. Audrey Sends Tentative Booking and Invoice Link

Message ID: `19e04c413cb4d7b3`
Date: Thu, 2026-05-07 16:26:58 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: Mira Park <mirasopa@gmail.com>
Subject: `Re: Form Submission - Wine tasting Booking`

Real email summary:
- Audrey says reservation is tentatively booked and held.
- Explains prepayment is required to confirm and finalize.
- Includes Square invoice link.
- Notes unpaid reservations may be released after due date.

Agent classification:
- `invoice_payment_message` or staff invoice/hold context, depending on current parser wording.

Agent state/action:
- After approved send, current implementation sets:
  - State: `WAITING_FOR_PAYMENT`
  - Payment status: `awaiting_invoice_marker`
  - Booking status: `tentative`
  - Recommended action: `review_payment_status`

### 14. Square Invoice Created

Message ID/thread ID: `19e04c04e220f935`
Date: Thu, 2026-05-07 23:22:50 UTC
From: Square <invoicing@messaging.squareup.com>
To: contact@innovatuswine.com
Subject: `A new invoice was created for Mira Park (#202440)`

Real email summary:
- Square says an invoice of $237.05 was sent to Mira Park.
- Invoice details include:
  - Party Name: Mira Park
  - Party of 2
  - Sunday, June 7, 2026, 2:30-4:00 PM PST
  - Venue: The Caves at Soda Canyon
  - Check-In Venue: Tasting Room

Agent classification:
- `invoice_payment_message`

Agent facts:
- `payment_status`: `sent`
- Client name: Mira Park
- Requested date/time can be extracted from Square body.

Agent state/action:
- State becomes `INVOICE_SENT`.
- Payment status becomes `sent`.
- Recommended action becomes `review_payment_status`.

### 15. Square Invoice Paid

Message ID/thread ID: `19e04cba91d71bb4`
Date: Thu, 2026-05-07 23:35:14 UTC
From: Square <invoicing@messaging.squareup.com>
To: contact@innovatuswine.com
Subject: `An invoice was paid by Mira Park! (#202440)`

Real email summary:
- Square says Mira Park paid invoice #202440 for $237.05.

Agent classification:
- `invoice_payment_message`

Agent facts:
- `payment_status`: `paid`

Agent state/action:
- State becomes `PAYMENT_RECEIVED`.
- Payment status becomes `paid`.
- Recommended action becomes `send_final_confirmation`.

Safety rule:
- The planner guard prevents final confirmation if payment status is not `paid`.

### 16. Final Confirmation Sent

Message ID: `19e1e44269f96304`
Thread ID: `19e1e362d0f43630`
Date: Tue, 2026-05-12 15:17:23 -0700
From: INNOVATUS <contact@innovatuswine.com>
To: Mira Park <mirasopa@gmail.com>
Subject: `Your Reservation at INNOVATUS is Confirmed - June 7`

Real email summary:
- Audrey confirms the reservation.
- Details:
  - INNOVATUS Tour and Wine Tasting with Winemaker
  - Party Name: Mira Park
  - Party of 2
  - Sunday, June 7, 2026, 2:30-4:00 PM PST
  - The Caves at Soda Canyon
  - Tasting Room

Agent classification:
- `final_confirmation_sent`

Agent state/action:
- State becomes `FINAL_CONFIRMED`.
- Booking status becomes `confirmed`.
- Payment status becomes `paid`.
- Recommended action becomes `None`.

## Current Agent Workflow Mapped To This Case

The agent's current intended automation flow for this real case is:

1. Gmail watcher finds the Squarespace form and invokes the current tasting-room coordinator.
2. `classify_and_extract` parses the form into client/date/time/party/experience facts.
3. `match_and_update_case` creates the reservation and source-backed requested-slot claim.
4. `persist_case_event` writes the reservation, claim, and event to Supabase.
5. `plan_case_action` may refine the next action from the case timeline.
6. `create_human_approval` creates a Telegram-gated action request.
7. Staff approves or marks decisions in the tasting room Telegram bot.
8. The agent sends approved emails through Gmail, or records internal/payment decisions.
9. Later Gmail replies update the same reservation through thread matching, contextual matching, or explicit reservation IDs.
10. Square invoice notifications drive payment state.
11. Final confirmation is only queued after payment is marked paid.

## What Worked Well In This Real Case Design

- The workflow has enough real signals to avoid relying on the LLM as source of truth.
- Josh availability is stored as claims instead of being overwritten into one mutable field.
- Square invoice created and paid emails are clean source-of-truth events for payment.
- Final confirmation has a clear payment guard.
- Telegram approvals give staff control over outbound messages and sensitive state changes.

## Current Gaps Exposed By This Real Case

1. Facility booking should be more explicit before invoice.

   In this real history, Audrey asks Josh to book and gets confirmation before treating the reservation as truly ready. Current code can go from `client_acceptance` directly to `send_tentative_invoice`. The safer state machine should require `facility_booking_requested` or `facility_confirmed` before invoice/hold email.

2. Cross-thread matching is necessary but fragile.

   The client thread, Josh availability thread, grouped confirmation thread, Square invoice threads, and final confirmation thread are separate Gmail threads. The agent currently relies on date/name/context matching for some of that. Future generated emails should include the `TASTING-*` reservation ID in subject/body to make matching deterministic.

3. Grouped Josh confirmations need careful parsing.

   Josh's grouped confirmation covers multiple bookings. The agent needs to extract only the matching 6/7, 2-person, 2:30 PM line for Mira and avoid applying it to unrelated reservations.

4. Staff-sent invoice/hold emails are not currently a first-class message type.

   The parser has `invoice_payment_message`, mostly aimed at Square notifications. It should probably distinguish:
   - staff tentative booking/invoice-link sent
   - Square invoice created
   - Square invoice paid

5. Source-backed claims should be used before re-asking Josh.

   When Mira asks for 6/7 after Josh has already said 6/7 2:30 is open, the agent should reuse the existing facility availability claim rather than drafting another Josh availability email.

## Suggested Target State Machine For This Case

```text
REQUEST_RECEIVED
  -> WAITING_FOR_CLIENT_REPLY
     because original requested date was unavailable

CLIENT_REQUESTED_ALTERNATIVE
  -> WAITING_FOR_JOSH
     after staff asks Josh about 6/6 and 6/7

FACILITY_AVAILABLE + INTERNAL_AVAILABLE
  -> READY_TO_OFFER_CLIENT
  -> SLOT_OFFERED_TO_CLIENT

CLIENT_REQUESTED_ALTERNATIVE
  -> READY_TO_OFFER_CLIENT
     if existing Josh claim already covers the new requested slot

SLOT_OFFERED_TO_CLIENT
  -> CLIENT_ACCEPTED_SLOT

CLIENT_ACCEPTED_SLOT
  -> NEEDS_FACILITY_BOOKING
  -> WAITING_FOR_JOSH_BOOKING_CONFIRMATION

JOSH_BOOKING_CONFIRMED
  -> TENTATIVELY_BOOKED
  -> send_tentative_invoice / review_payment_status

INVOICE_SENT
  -> WAITING_FOR_PAYMENT

PAYMENT_RECEIVED
  -> send_final_confirmation

FINAL_CONFIRMED
  -> closed
```

## Files In The Agent That Implement This Flow

- Gmail polling: `services/tastingroom_mailbox.py`
- Tasting-room coordinator: `vertex_agent/intake.py`
- Classification/extraction/state machine/actions: `services/tastingroom_service.py`
- Approval channel: Google Chat tasting-room app
- Staff command helpers: `services/tastingroom_chat_service.py`
- Persistence: `db/repository.py`
- Tables: `db/schema.sql`
- Historical script with this real case's Gmail IDs: `scripts/replay_mira_case.py`

## Quick Review Checklist

- Does every outbound email require a Telegram approval action? Yes.
- Does final confirmation require paid status? Yes, guarded.
- Does the current flow require Josh booking confirmation before invoice? Not strongly enough.
- Does the current flow handle multiple Gmail threads for one reservation? Partially, through thread/context/name matching.
- Would explicit reservation IDs improve reliability? Yes.
- Is this a real case from Lisa's Gmail? Yes. The message IDs and metadata above were read from Lisa's Gmail token on 2026-05-29.
