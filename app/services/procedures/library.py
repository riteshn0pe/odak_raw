"""The shipped procedure template gallery: twelve SOPs as data, plus ten presets.

`LIBRARY` is the gallery of §3 — the twelve authored templates — and `PRESETS`
is the ten wording variants §3.13 accounts for. Both ship as code rather than as
rows in a platform table, for the same reason the action library does: shipping a
new template should be a deploy, not a data migration, and a platform table
holding tenant-facing rows would need RLS it cannot have. Installing a template
writes a real `procedures` row the tenant then edits freely; `library_key`
records which gallery entry seeded it and there is no live link back.

**Field names are the `procedures` column names.** Every field on
`ProcedureTemplate` that a `procedures` row can hold is named exactly after its
column in `migrations/010_procedures_contacts.sql`, so an installer inserts one
without a translation layer. Two columns are deliberately absent:

* `required_actions uuid[]` is resolved at publish time from the `@name` tokens
  in the step text (§1.5). The gallery cannot know a tenant's action ids, so it
  carries `action_names` instead — the union of names the installer resolves.
* `tenant_id`, `agent_id`, `created_by` and the timestamps belong to the install,
  not to the template.

**Step shape.** `steps` holds the plain JSON the `steps jsonb` column stores,
using §1.2's step vocabulary and §1.6's branch grammar verbatim. An instruction
step carries `sid` (the `step_sid` `advance_procedure` reports), `text`, the
parsed `@action` references, and the per-step availability policy of §1.5. A
decision step carries `branches`, each with §1.6's `label` / `condition_text` /
optional `guard`, and instruction-only children — depth is capped at 1, so a
branch child is never itself a decision.

`on_unavailable` is `None` wherever §3 does not state one: null means "use the
§1.5 default for the referenced action's kind", which is a decision the save path
makes once it has resolved the action, not one the gallery can make blind.
`action_id` on a reference is likewise `None` — the id is filled in at save time,
the name is what the author wrote.

The top-level `steps` field is a tuple so the module-level records are shallowly
immutable; `json.dumps` renders a tuple as a JSON array, so this costs the
installer nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.services.actions import library as action_library

# One step or branch as the `steps` column holds it. Typed loosely on purpose:
# the authoritative schema is another module's Pydantic model, and duplicating it
# here as a TypedDict would be a second definition to keep in step with the first.
StepJson = dict[str, Any]
BranchJson = dict[str, Any]

# §1.6: the branch label is an enum so the model cannot invent an arm.
BRANCH_LABELS = ("b1", "b2", "b3", "b4", "otherwise")


def _refs(*names: str) -> list[StepJson]:
    """`@name` tokens as §1.5 stores them, with the id left for save time."""
    return [{"action_name": name, "action_id": None} for name in names]


def _step(
    sid: str,
    text: str,
    *,
    actions: tuple[str, ...] = (),
    required: bool = False,
    requires_confirmation: bool = False,
    on_unavailable: str | None = None,
) -> StepJson:
    """One instruction step. Reads as a table at the call sites below."""
    return {
        "sid": sid,
        "kind": "instruction",
        "text": text,
        "actions": _refs(*actions),
        "required": required,
        "requires_confirmation": requires_confirmation,
        "on_unavailable": on_unavailable,
    }


def _branch(
    label: str,
    condition_text: str,
    steps: tuple[StepJson, ...],
    *,
    guard: str | None = None,
) -> BranchJson:
    """One arm of a decision point, per §1.6."""
    if label not in BRANCH_LABELS:
        raise ValueError(f"branch label {label!r} is not one of {BRANCH_LABELS}")
    return {
        "label": label,
        "condition_text": condition_text,
        "guard": guard,
        "steps": list(steps),
    }


def _decision(sid: str, text: str, branches: tuple[BranchJson, ...]) -> StepJson:
    """A decision point: depth 1, at most five arms including `otherwise`."""
    return {"sid": sid, "kind": "decision", "text": text, "branches": list(branches)}


@dataclass(frozen=True, slots=True)
class ProcedureTemplate:
    """One gallery entry, ready to insert as a `procedures` row.

    `library_key`, `name`, `trigger_text`, `steps`,
    `redact_transcript_on_escalation`, `enabled` and `version` are the column
    names. `installed_by` and `action_names` are install metadata, not columns:
    the first records which agent templates offer this procedure, the second is
    what the installer resolves into `required_actions`.

    `enabled` is False on every entry. A procedure with an unresolved action
    reference cannot be enabled at all (§1.5), and most of these reference an
    action that needs a credential first, so the installer turns the row on once
    the references resolve rather than shipping an SOP that dead-ends.
    """

    library_key: str
    name: str
    trigger_text: str
    steps: tuple[StepJson, ...]
    action_names: tuple[str, ...]
    installed_by: tuple[str, ...]
    redact_transcript_on_escalation: bool = False
    enabled: bool = False
    version: int = 1


# ------------------------------------------------------------ 3.1 refund_request

REFUND_REQUEST = ProcedureTemplate(
    library_key="refund_request",
    name="Refund request",
    trigger_text=(
        "when a customer asks for a refund, their money back, a chargeback, or to "
        "cancel an order they have already paid for"
    ),
    action_names=("lookup_order", "issue_refund", "escalate_to_human"),
    installed_by=("customer-support", "order-returns"),
    steps=(
        _step(
            "s1",
            "Ask for the order number. If they do not have it, ask for the email "
            "address they used at checkout.",
        ),
        _step(
            "s2",
            "Look the order up with @lookup_order. If the lookup returns nothing, "
            "tell them you cannot find it, ask them to check the number, and if it "
            "still fails hand over with @escalate_to_human.",
            actions=("lookup_order", "escalate_to_human"),
            required=True,
            on_unavailable="escalate",
        ),
        _step(
            "s3",
            "Read the order back to them — what was ordered, the date, and the "
            "total — and ask them to confirm it is the right one.",
        ),
        _step("s4", "Ask why they want a refund. Ask once; do not interrogate."),
        _decision(
            "s5",
            "Decision — is this refundable?",
            (
                _branch(
                    "b1",
                    "the order was placed less than 30 days ago and the item is not excluded",
                    (
                        _step(
                            "s5.b1.1",
                            "Confirm the exact amount you are about to refund: "
                            "{{order.total}}.",
                        ),
                        _step(
                            "s5.b1.2",
                            "Issue it with @issue_refund.",
                            actions=("issue_refund",),
                            required=True,
                            requires_confirmation=True,
                        ),
                        _step(
                            "s5.b1.3",
                            "Tell them it will show on their statement in 5-7 working days.",
                        ),
                    ),
                    guard=(
                        "{{order.placed_at}} > now-30d and "
                        "{{order.category}} != 'consumable'"
                    ),
                ),
                _branch(
                    "b2",
                    "the order is outside the 30-day window, or the item is excluded",
                    (
                        _step(
                            "s5.b2.1",
                            "Say which condition is not met and quote the returns policy "
                            "on that point. Do not paraphrase the policy.",
                        ),
                        _step(
                            "s5.b2.2",
                            "Offer to pass it to the team if they would like it looked at "
                            "anyway, and hand over with @escalate_to_human if they say yes.",
                            actions=("escalate_to_human",),
                        ),
                    ),
                ),
                _branch(
                    "otherwise",
                    "you could not establish the date, or the policy does not cover this",
                    (
                        _step(
                            "s5.otherwise.1",
                            "Do not decide. Say a colleague will review it and hand over "
                            "with @escalate_to_human, including the order number.",
                            actions=("escalate_to_human",),
                            required=True,
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# -------------------------------------------------------- 3.2 where_is_my_order

WHERE_IS_MY_ORDER = ProcedureTemplate(
    library_key="where_is_my_order",
    name="Where is my order",
    trigger_text=(
        "when a customer asks where their order is, when it will arrive, or about "
        "tracking or delivery"
    ),
    action_names=("lookup_order", "custom_button", "escalate_to_human"),
    installed_by=("customer-support", "order-returns"),
    steps=(
        _step("s1", "Ask for the order number, or the email address used at checkout."),
        _step(
            "s2",
            "Look it up with @lookup_order. If nothing comes back, say you cannot "
            "find it and ask them to check the number.",
            actions=("lookup_order",),
            required=True,
        ),
        _step(
            "s3",
            "Tell them exactly what the record says — what was ordered, the status, "
            "the carrier, and the expected delivery date. Do not estimate a date the "
            "record does not give.",
        ),
        _decision(
            "s4",
            "Decision — what does the record show?",
            (
                _branch(
                    "b1",
                    "there is a tracking link or number",
                    (
                        _step(
                            "s4.b1.1",
                            "Give them the tracking number and offer the link with "
                            "@custom_button.",
                            actions=("custom_button",),
                            on_unavailable="skip",
                        ),
                    ),
                    guard="exists {{order.tracking_url}}",
                ),
                _branch(
                    "b2",
                    "the expected delivery date has already passed",
                    (
                        _step(
                            "s4.b2.1",
                            "Apologise once. Say you are passing it to the team, then hand "
                            "over with @escalate_to_human, including the order number and "
                            "the carrier.",
                            actions=("escalate_to_human",),
                            required=True,
                        ),
                    ),
                    guard="{{order.expected_at}} < now-0d",
                ),
                _branch(
                    "otherwise",
                    "the record shows neither a tracking link nor a missed date",
                    (
                        _step(
                            "s4.otherwise.1",
                            "Confirm the expected date and ask if there is anything else.",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# ----------------------------------------------------------- 3.3 return_request
#
# Kept distinct from `refund_request` because a return moves physical goods: it
# needs an RMA reference, a return address or label, and the refund happens only
# after the item arrives.

RETURN_REQUEST = ProcedureTemplate(
    library_key="return_request",
    name="Return request",
    trigger_text=(
        "when a customer wants to send something back, asks for a returns label, or "
        "asks how returns work for an order they have already received"
    ),
    action_names=("lookup_order", "start_return", "escalate_to_human"),
    installed_by=("order-returns",),
    steps=(
        _step("s1", "Ask which order it is — the order number, or the checkout email."),
        _step(
            "s2",
            "Look it up with @lookup_order.",
            actions=("lookup_order",),
            required=True,
        ),
        _step("s3", "Ask which item they are returning, and why, in one sentence."),
        _step(
            "s4",
            "State the return window and the condition requirements exactly as the "
            "returns policy words them.",
        ),
        _decision(
            "s5",
            "Decision — is this returnable?",
            (
                _branch(
                    "b1",
                    "within the window and not excluded",
                    (
                        _step(
                            "s5.b1.1",
                            "Start the return with @start_return, using the order id and "
                            "the item.",
                            actions=("start_return",),
                            required=True,
                            requires_confirmation=True,
                        ),
                        _step(
                            "s5.b1.2",
                            "Give them the return reference and the address or label "
                            "instructions exactly as returned.",
                        ),
                        _step(
                            "s5.b1.3",
                            "Explain that the refund is processed once the item arrives "
                            "back, and how long that takes.",
                        ),
                    ),
                    guard=(
                        "{{order.delivered_at}} > now-30d and "
                        "{{order.category}} != 'consumable'"
                    ),
                ),
                _branch(
                    "b2",
                    "outside the window or excluded",
                    (
                        _step(
                            "s5.b2.1",
                            "Say which condition fails, quote the policy, and offer to "
                            "pass it to the team.",
                        ),
                    ),
                ),
                _branch(
                    "otherwise",
                    "you cannot establish the delivery date or whether the item is excluded",
                    (
                        _step(
                            "s5.otherwise.1",
                            "Hand over with @escalate_to_human.",
                            actions=("escalate_to_human",),
                            required=True,
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# ------------------------------------------------------- 3.4 damaged_item_claim
#
# No write to a third party: this one always ends with a person.

DAMAGED_ITEM_CLAIM = ProcedureTemplate(
    library_key="damaged_item_claim",
    name="Damaged item claim",
    trigger_text=(
        "when a customer says an item arrived damaged, broken, faulty, leaking, or "
        "that they were sent the wrong thing"
    ),
    action_names=("lookup_order", "collect_data", "escalate_to_human"),
    installed_by=("customer-support", "order-returns"),
    steps=(
        _step(
            "s1",
            "Apologise once, in one sentence. Do not apologise again in every message.",
        ),
        _step(
            "s2",
            "Ask for the order number or the checkout email, and look it up with "
            "@lookup_order.",
            actions=("lookup_order",),
            on_unavailable="ask",
        ),
        _step("s3", "Ask which item is affected and what is wrong with it."),
        _step(
            "s4",
            "Ask for a photo if the channel supports one. If it does not, ask them to "
            "describe the damage.",
            on_unavailable="skip",
        ),
        _step(
            "s5",
            "Record it all with @collect_data so the team has the order, the item, the "
            "fault and the photo reference in one place.",
            actions=("collect_data",),
            required=True,
        ),
        _step(
            "s6",
            "Hand over with @escalate_to_human at high urgency. Do not promise a "
            "replacement, a refund, or a collection — the team decides that.",
            actions=("escalate_to_human",),
            required=True,
        ),
    ),
)

# ------------------------------------------------------------- 3.5 cannot_log_in

CANNOT_LOG_IN = ProcedureTemplate(
    library_key="cannot_log_in",
    name="Cannot log in",
    trigger_text=(
        "when someone cannot sign in, is locked out, says their password does not "
        "work, or is not receiving a login code"
    ),
    action_names=("collect_data", "escalate_to_human"),
    installed_by=("customer-support", "product-docs", "it-helpdesk"),
    steps=(
        _step(
            "s1",
            "Never ask for, accept, or repeat a password, a one-time code, or a "
            "recovery code. If they send one anyway, tell them to change it "
            "immediately.",
        ),
        _step(
            "s2",
            "Ask which email address they are trying to sign in with, and what exactly "
            "happens — the wording of the error, or nothing at all.",
        ),
        _decision(
            "s3",
            "Decision — what kind of failure is this?",
            (
                _branch(
                    "b1",
                    'they get "no account found" or the email is not recognised',
                    (
                        _step(
                            "s3.b1.1",
                            "Ask whether they might have signed up with a different "
                            "address, or through Google or a work account.",
                        ),
                    ),
                ),
                _branch(
                    "b2",
                    "the password is rejected",
                    (
                        _step(
                            "s3.b2.1",
                            "Walk them through the password reset link on the sign-in "
                            "page, in the words the documentation uses.",
                        ),
                        _step(
                            "s3.b2.2",
                            "Ask them to check the spam folder if the email does not "
                            "arrive within a few minutes.",
                        ),
                    ),
                ),
                _branch(
                    "b3",
                    "the code or reset email never arrives",
                    (
                        _step(
                            "s3.b3.1",
                            "Confirm the address is spelled correctly, then check whether "
                            "they can open that mailbox at all right now.",
                        ),
                    ),
                ),
                _branch(
                    "otherwise",
                    "you cannot tell which kind of failure this is",
                    (
                        _step(
                            "s3.otherwise.1",
                            "Ask them to describe what they see on screen.",
                        ),
                    ),
                ),
            ),
        ),
        _step(
            "s4",
            "If it still does not work, record the email and the exact error with "
            "@collect_data and hand over with @escalate_to_human.",
            actions=("collect_data", "escalate_to_human"),
            required=True,
        ),
    ),
)

# ---------------------------------------------------------- 3.6 vpn_wont_connect

VPN_WONT_CONNECT = ProcedureTemplate(
    library_key="vpn_wont_connect",
    name="VPN won't connect",
    trigger_text="when a colleague cannot connect to the VPN, or the VPN drops repeatedly",
    action_names=("collect_data", "escalate_to_human"),
    installed_by=("it-helpdesk", "developer-support"),
    steps=(
        _step(
            "s1",
            "Ask which VPN client and version, which operating system, and the exact "
            "error text.",
        ),
        _step(
            "s2",
            "Check the basics with them, one at a time, and ask what happened after "
            "each: is the internet working otherwise; sign out and back in; restart "
            "the client; restart the machine.",
        ),
        _decision(
            "s3",
            "Decision — what does the error point to?",
            (
                _branch(
                    "b1",
                    "a certificate or authentication error",
                    (
                        _step(
                            "s3.b1.1",
                            "Do not attempt a fix. Record the error with @collect_data "
                            "and raise it with IT via @escalate_to_human — certificates "
                            "are re-issued by hand.",
                            actions=("collect_data", "escalate_to_human"),
                            required=True,
                        ),
                    ),
                ),
                _branch(
                    "b2",
                    "it works on a phone hotspot but not on their home wifi",
                    (
                        _step(
                            "s3.b2.1",
                            "Explain that their router or ISP is blocking the connection, "
                            "and give the documented workaround.",
                        ),
                    ),
                ),
                _branch(
                    "otherwise",
                    "the error points at nothing specific",
                    (
                        _step(
                            "s3.otherwise.1",
                            "If you have been round the checks twice with no change, "
                            "stop. Record what was tried with @collect_data and raise a "
                            "ticket with @escalate_to_human. Do not loop through the same "
                            "checks a third time.",
                            actions=("collect_data", "escalate_to_human"),
                            required=True,
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# --------------------------------------------------------- 3.7 new_starter_setup
#
# Internal-facing.

NEW_STARTER_SETUP = ProcedureTemplate(
    library_key="new_starter_setup",
    name="New starter setup",
    trigger_text=(
        "when a manager or someone in HR asks to set up accounts and equipment for a "
        "new starter"
    ),
    action_names=("collect_data", "notify_slack", "escalate_to_human"),
    installed_by=("it-helpdesk",),
    steps=(
        _step(
            "s1",
            "Ask for the new starter's full name, their start date, their job title, "
            "and which team they are joining.",
        ),
        _step(
            "s2",
            "Ask who their manager is, and whether they need a laptop or will use "
            "their own.",
        ),
        _decision(
            "s3",
            "Decision — which access set applies?",
            (
                _branch(
                    "b1",
                    "the role needs standard access only",
                    (
                        _step(
                            "s3.b1.1",
                            "List the standard account set from the onboarding "
                            "documentation, in the documentation's own words, and confirm "
                            "it with them.",
                        ),
                    ),
                ),
                _branch(
                    "b2",
                    "the role needs privileged or finance access",
                    (
                        _step(
                            "s3.b2.1",
                            "Say that privileged access needs a named approver, and ask "
                            "who is approving it.",
                        ),
                        _step(
                            "s3.b2.2",
                            "Hand over with @escalate_to_human — you do not raise "
                            "privileged access yourself.",
                            actions=("escalate_to_human",),
                            required=True,
                        ),
                    ),
                ),
                _branch(
                    "otherwise",
                    "you cannot tell which access set the role needs",
                    (
                        _step(
                            "s3.otherwise.1",
                            "Ask what systems the role needs and hand over.",
                            actions=("escalate_to_human",),
                        ),
                    ),
                ),
            ),
        ),
        _step(
            "s4",
            "Record everything gathered with @collect_data.",
            actions=("collect_data",),
            required=True,
        ),
        _step(
            "s5",
            "Post the request to the IT channel with @notify_slack so it is picked up "
            "before the start date.",
            actions=("notify_slack",),
            on_unavailable="escalate",
        ),
        _step(
            "s6",
            "Confirm back what you have logged and by when it will be actioned. Do not "
            "confirm that any account has been created — you have raised the request, "
            "not fulfilled it.",
        ),
    ),
)

# ------------------------------------------------------------- 3.8 leave_request
#
# Kept as a procedure rather than cut to retrieval because the answer genuinely
# branches on contract type, and getting that branch wrong is an
# employment-relations problem.

LEAVE_REQUEST = ProcedureTemplate(
    library_key="leave_request",
    name="Leave request question",
    trigger_text=(
        "when a colleague asks how to book leave, how much leave they have, or about "
        "sickness, parental or unpaid leave"
    ),
    action_names=("collect_data", "escalate_to_human"),
    installed_by=("hr-policy",),
    steps=(
        _step(
            "s1",
            "Ask what kind of leave they mean — annual, sick, parental, or unpaid.",
        ),
        _step(
            "s2",
            "Ask whether they are on a permanent contract, a fixed-term contract, or "
            "working through an agency. The entitlement differs and you must not guess.",
        ),
        _decision(
            "s3",
            "Decision — which policy applies?",
            (
                _branch(
                    "b1",
                    "permanent",
                    (
                        _step(
                            "s3.b1.1",
                            "Quote the entitlement and the booking process from the "
                            "handbook, word for word.",
                        ),
                    ),
                ),
                _branch(
                    "b2",
                    "fixed-term or part-time",
                    (
                        _step(
                            "s3.b2.1",
                            "Quote the pro-rata rule from the handbook and say plainly "
                            "that the exact number depends on their contracted hours, "
                            "which you cannot see.",
                        ),
                    ),
                ),
                _branch(
                    "b3",
                    "agency or contractor",
                    (
                        _step(
                            "s3.b3.1",
                            "Say the handbook does not cover their arrangement and hand "
                            "over with @escalate_to_human.",
                            actions=("escalate_to_human",),
                            required=True,
                        ),
                    ),
                ),
                _branch(
                    "otherwise",
                    "you cannot tell which contract type applies",
                    (
                        _step(
                            "s3.otherwise.1",
                            "Hand over with @escalate_to_human.",
                            actions=("escalate_to_human",),
                            required=True,
                        ),
                    ),
                ),
            ),
        ),
        _step(
            "s4",
            "Never state a specific remaining balance. You do not have access to their "
            "record. If they ask for a balance, record the request with @collect_data "
            "and pass it to HR.",
            actions=("collect_data",),
        ),
    ),
)

# ------------------------------------------------- 3.9 escalate_sensitive_matter
#
# `redact_transcript_on_escalation` is True here by design: the escalation carries
# the routing fields and step 3's answers only, not the full transcript. It is a
# procedure-level flag rather than a per-step action-argument override so that a
# tenant editing step 4 cannot accidentally turn transcript egress back on.

ESCALATE_SENSITIVE_MATTER = ProcedureTemplate(
    library_key="escalate_sensitive_matter",
    name="Escalate a sensitive matter",
    trigger_text=(
        "when someone raises harassment, discrimination, bullying, a safety concern, a "
        "safeguarding concern, a legal threat, or says they are in distress"
    ),
    action_names=("escalate_to_human",),
    installed_by=("hr-policy", "customer-support", "appointment-booking", "it-helpdesk"),
    redact_transcript_on_escalation=True,
    steps=(
        _step(
            "s1",
            "Stop using the documents. Do not quote a policy at them as your first "
            "response.",
        ),
        _step(
            "s2",
            "Say, in one or two sentences, that you have heard them, that it will be "
            "taken seriously, and that you are passing it to a person now.",
        ),
        _step(
            "s3",
            "Ask only what you need to route it: whether they are willing to give their "
            "name, and whether it is urgent. Ask nothing else. Do not ask them to "
            "describe it again.",
        ),
        _step(
            "s4",
            "Hand over with @escalate_to_human at high urgency, to the named "
            "destination configured for sensitive matters.",
            actions=("escalate_to_human",),
            required=True,
        ),
        _step(
            "s5",
            "Never call @collect_data on what they told you. Never call @notify_slack. "
            "Never name a colleague they mentioned.",
        ),
        _step("s6", "Tell them who will be in touch and roughly when, then stop."),
    ),
)

# ------------------------------------------------------------ 3.10 take_a_booking

TAKE_A_BOOKING = ProcedureTemplate(
    library_key="take_a_booking",
    name="Take a booking",
    trigger_text=(
        "when someone wants to book, reschedule or cancel an appointment, a viewing or "
        "a slot"
    ),
    action_names=("check_availability", "book_meeting", "custom_button", "escalate_to_human"),
    installed_by=("appointment-booking", "property-enquiry"),
    steps=(
        _step(
            "s1",
            "Ask what they need booked, and roughly when suits them — which days, and "
            "morning or afternoon.",
        ),
        _step(
            "s2",
            "Get real availability with @check_availability for that service and "
            "window. Never invent a slot.",
            actions=("check_availability",),
            required=True,
        ),
        _step(
            "s3",
            "Offer up to three specific times. If none work, ask for a different window "
            "and check again — at most twice, then offer to have someone call them.",
        ),
        _step(
            "s4",
            "Take their name and phone number, and email if the booking needs one. If "
            "they are signed in, use the details you already have and only confirm them.",
        ),
        _step(
            "s5",
            "Read the whole booking back — service, date, time, name — and ask them to "
            "confirm.",
        ),
        _step(
            "s6",
            "Book it with @book_meeting.",
            actions=("book_meeting",),
            required=True,
            requires_confirmation=True,
        ),
        _decision(
            "s7",
            "Decision — did the calendar confirm?",
            (
                _branch(
                    "b1",
                    "confirmed",
                    (
                        _step(
                            "s7.b1.1",
                            "Give the reference and say what happens next. Offer the "
                            "calendar link with @custom_button.",
                            actions=("custom_button",),
                            on_unavailable="skip",
                        ),
                    ),
                    guard="exists {{booking.reference}}",
                ),
                _branch(
                    "otherwise",
                    "not confirmed",
                    (
                        _step(
                            "s7.otherwise.1",
                            "Do not say it is booked. Say the system did not confirm, "
                            "that you have kept their details, and hand over with "
                            "@escalate_to_human.",
                            actions=("escalate_to_human",),
                            required=True,
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# --------------------------------------------------------------- 3.11 book_a_demo

BOOK_A_DEMO = ProcedureTemplate(
    library_key="book_a_demo",
    name="Book a demo",
    trigger_text=(
        "when someone asks for a demo, a call, a trial walkthrough, or to speak to sales"
    ),
    action_names=("check_availability", "book_meeting", "collect_lead", "notify_slack"),
    installed_by=("sales-enquiries",),
    steps=(
        _step("s1", "Ask what they most want to see. One question, not a questionnaire."),
        _step("s2", "Ask how many people will join and what their roles are."),
        _step(
            "s3",
            "Take their name, work email, and company with @collect_lead.",
            actions=("collect_lead",),
            required=True,
        ),
        _step(
            "s4",
            "Offer times from @check_availability, in their timezone.",
            actions=("check_availability",),
            required=True,
        ),
        _step(
            "s5",
            "Read the details back, then book with @book_meeting.",
            actions=("book_meeting",),
            required=True,
            requires_confirmation=True,
        ),
        _decision(
            "s6",
            "Decision — did it confirm?",
            (
                _branch(
                    "b1",
                    "confirmed",
                    (
                        _step(
                            "s6.b1.1",
                            "Give the reference and say the invitation is on its way.",
                        ),
                    ),
                    guard="exists {{booking.reference}}",
                ),
                _branch(
                    "otherwise",
                    "not confirmed",
                    (
                        _step(
                            "s6.otherwise.1",
                            "Say the calendar did not confirm, that you have their "
                            "details, and notify the team with @notify_slack so a person "
                            "books it by hand.",
                            actions=("notify_slack",),
                            required=True,
                            on_unavailable="escalate",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# -------------------------------------------------------- 3.12 qualify_new_enquiry

QUALIFY_NEW_ENQUIRY = ProcedureTemplate(
    library_key="qualify_new_enquiry",
    name="Qualify a new enquiry",
    trigger_text=(
        "when someone new asks what you do, what it costs, or whether you would be a "
        "fit for them"
    ),
    action_names=("collect_lead", "escalate_to_human"),
    installed_by=("sales-enquiries", "property-enquiry", "admissions"),
    steps=(
        _step(
            "s1",
            "Answer their actual question first, from the documents, before you ask "
            "anything back.",
        ),
        _step(
            "s2",
            "Ask what they are trying to solve. One question at a time — never a list.",
        ),
        _step(
            "s3",
            "Ask about the two qualifying facts the documents say matter for fit (for "
            "example size and timescale), one at a time.",
        ),
        _decision(
            "s4",
            "Decision — do they meet the criteria in the documents?",
            (
                _branch(
                    "b1",
                    "they clearly fit",
                    (
                        _step(
                            "s4.b1.1",
                            "Say so plainly and why, quoting the relevant criterion.",
                        ),
                        _step(
                            "s4.b1.2",
                            "Take their details with @collect_lead and offer a call.",
                            actions=("collect_lead",),
                            required=True,
                        ),
                    ),
                ),
                _branch(
                    "b2",
                    "they clearly do not fit",
                    (
                        _step(
                            "s4.b2.1",
                            "Say so honestly and say what the criterion is. Do not push "
                            "them towards a call anyway.",
                        ),
                        _step(
                            "s4.b2.2",
                            "Point them at whatever the documents suggest instead, if "
                            "anything.",
                        ),
                    ),
                ),
                _branch(
                    "otherwise",
                    "you cannot tell from the documents",
                    (
                        _step(
                            "s4.otherwise.1",
                            "Say a colleague will confirm, take their details with "
                            "@collect_lead, and hand over with @escalate_to_human.",
                            actions=("collect_lead", "escalate_to_human"),
                            required=True,
                        ),
                    ),
                ),
            ),
        ),
    ),
)


# ------------------------------------------------------------------ the gallery

LIBRARY: tuple[ProcedureTemplate, ...] = (
    REFUND_REQUEST,
    WHERE_IS_MY_ORDER,
    RETURN_REQUEST,
    DAMAGED_ITEM_CLAIM,
    CANNOT_LOG_IN,
    VPN_WONT_CONNECT,
    NEW_STARTER_SETUP,
    LEAVE_REQUEST,
    ESCALATE_SENSITIVE_MATTER,
    TAKE_A_BOOKING,
    BOOK_A_DEMO,
    QUALIFY_NEW_ENQUIRY,
)

BY_KEY: Mapping[str, ProcedureTemplate] = {item.library_key: item for item in LIBRARY}


# --------------------------------------------------------------------- presets


@dataclass(frozen=True, slots=True)
class ProcedurePreset:
    """A wording variant of a gallery template, per §3.13's dispositions.

    A preset is not a thirteenth template: it is a base plus deltas, which is why
    §3 ships twelve authored flows and ten presets rather than twenty-two. The
    deltas are deliberately narrow — a different trigger, replacement text on
    named steps, replacement condition text on named branch arms, dropped tail
    steps, and appended ones. Anything needing a different control flow is a
    template, not a preset.

    `installable(key)` materialises one into a full `ProcedureTemplate`, so an
    installer treats presets and templates identically.

    Override keys are step sids (`"s2"`, `"s4.b2.1"`) for text, and
    `"<decision sid>.<label>"` (`"s4.b1"`) for branch conditions.
    """

    library_key: str
    name: str
    base_key: str
    trigger_text: str
    installed_by: tuple[str, ...]
    step_text_overrides: tuple[tuple[str, str], ...] = ()
    branch_condition_overrides: tuple[tuple[str, str], ...] = ()
    dropped_step_sids: tuple[str, ...] = ()
    appended_steps: tuple[StepJson, ...] = ()
    redact_transcript_on_escalation: bool | None = None


PRESETS: tuple[ProcedurePreset, ...] = (
    ProcedurePreset(
        library_key="password_reset",
        name="Password reset",
        base_key="cannot_log_in",
        trigger_text="when a colleague needs a password reset for an internal system",
        installed_by=("it-helpdesk",),
        step_text_overrides=(
            (
                "s2",
                "Ask which email address they are trying to sign in with, which system "
                "they need the reset for, what exactly happens — the wording of the "
                "error, or nothing at all — and whether they can still reach their "
                "email on another device, because that determines which reset route "
                "works.",
            ),
            (
                "s4",
                "If it still does not work, record the email, the system and the exact "
                "error with @collect_data and hand over with @escalate_to_human to the "
                "IT queue.",
            ),
        ),
    ),
    ProcedurePreset(
        library_key="report_security_concern",
        name="Report a security concern",
        base_key="escalate_sensitive_matter",
        trigger_text=(
            "when someone reports a phishing email, a suspected breach, a lost device, "
            "or shared credentials"
        ),
        installed_by=("it-helpdesk",),
        step_text_overrides=(
            (
                "s3",
                "Ask only what you need to route it: whether they are willing to give "
                "their name, whether it is urgent, and whether anything was clicked or "
                "entered. Ask nothing else. Do not ask them to describe it again.",
            ),
            (
                "s4",
                "Hand over with @escalate_to_human at high urgency, to the security "
                "queue.",
            ),
        ),
    ),
    ProcedurePreset(
        library_key="urgent_enquiry",
        name="Urgent enquiry",
        base_key="escalate_sensitive_matter",
        trigger_text="when someone describes an urgent medical or welfare situation",
        installed_by=("appointment-booking",),
        step_text_overrides=(
            (
                "s2",
                "Give the configured emergency line first — if this is an emergency, "
                "tell them to call it now. Then say, in one or two sentences, that you "
                "have heard them, that it will be taken seriously, and that you are "
                "passing it to a person now.",
            ),
        ),
    ),
    ProcedurePreset(
        library_key="change_or_cancel_booking",
        name="Change or cancel a booking",
        base_key="take_a_booking",
        trigger_text="when someone wants to move or cancel an existing booking",
        installed_by=("appointment-booking",),
        step_text_overrides=(
            (
                "s1",
                "Ask for the booking reference, or the name and date it was booked "
                "under, and look it up first.",
            ),
        ),
    ),
    ProcedurePreset(
        library_key="book_a_viewing",
        name="Book a viewing",
        base_key="take_a_booking",
        trigger_text=(
            "when someone wants to book, reschedule or cancel a viewing of a property"
        ),
        installed_by=("property-enquiry",),
        step_text_overrides=(
            (
                "s1",
                "Ask which property they want to view, and roughly when suits them — "
                "which days, and morning or afternoon.",
            ),
            (
                "s2",
                "Get real availability with @check_availability for that property and "
                "window. Never invent a slot.",
            ),
            (
                "s4",
                "Take their name and phone number, and email if the viewing needs one, "
                "and ask whether anyone is coming with them. If they are signed in, use "
                "the details you already have and only confirm them.",
            ),
            (
                "s5",
                "Read the whole viewing back — property, date, time, name — and ask "
                "them to confirm.",
            ),
        ),
    ),
    ProcedurePreset(
        library_key="new_property_enquiry",
        name="New property enquiry",
        base_key="qualify_new_enquiry",
        trigger_text=(
            "when someone new asks what is available, what an area is like, or what a "
            "property would cost them"
        ),
        installed_by=("property-enquiry",),
        step_text_overrides=(
            (
                "s3",
                "Ask about the two qualifying facts the documents say matter for fit "
                "(for example budget and the area they want), one at a time.",
            ),
        ),
    ),
    ProcedurePreset(
        library_key="no_match_capture_requirements",
        name="No match — capture requirements",
        base_key="qualify_new_enquiry",
        trigger_text=(
            "when someone is looking for something and nothing on the books matches "
            "what they described"
        ),
        installed_by=("property-enquiry",),
        dropped_step_sids=("s4",),
        appended_steps=(
            _step(
                "s4",
                "Say plainly that nothing on the books matches right now. Take their "
                "details with @collect_lead, together with a note of exactly what they "
                "were looking for, and say you will come back to them when something "
                "matches.",
                actions=("collect_lead",),
                required=True,
            ),
        ),
    ),
    ProcedurePreset(
        library_key="check_eligibility",
        name="Check eligibility",
        base_key="qualify_new_enquiry",
        trigger_text=(
            "when someone asks whether they are eligible to apply, or what the entry "
            "requirements are"
        ),
        installed_by=("admissions",),
        step_text_overrides=(
            (
                "s3",
                "Ask the entry criteria the published requirements set out — their "
                "qualifications, then their year of entry — one at a time.",
            ),
            (
                "s4.b2.1",
                "Say so honestly and quote the published criteria verbatim. Do not push "
                "them towards a call anyway.",
            ),
        ),
        branch_condition_overrides=(
            ("s4.b1", "they are eligible"),
            ("s4.b2", "they are not eligible"),
            ("s4.otherwise", "eligibility needs a human to confirm"),
        ),
    ),
    ProcedurePreset(
        library_key="integration_not_working",
        name="Integration not working",
        base_key="vpn_wont_connect",
        trigger_text=(
            "when a developer says an integration, an API call, a webhook or an SDK is "
            "not working"
        ),
        installed_by=("developer-support",),
        step_text_overrides=(
            (
                "s1",
                "Ask which integration and endpoint, which SDK and version, and the "
                "exact error text or status code.",
            ),
            (
                "s2",
                "Work through the documented runbook with them, one check at a time, "
                "and ask what happened after each: is the base URL right; is the key "
                "the live one; retry the call unchanged; try the same call from the "
                "documented example.",
            ),
            (
                "s3.b1.1",
                "Do not attempt a fix. Record the error with @collect_data and raise it "
                "with the team via @escalate_to_human — credentials are re-issued by "
                "hand.",
            ),
            (
                "s3.b2.1",
                "Explain that the difference is environmental, and give the documented "
                "workaround for that environment.",
            ),
        ),
        branch_condition_overrides=(
            ("s3.b1", "an authentication, key or permission error"),
            ("s3.b2", "it works in one environment but not in another"),
            ("s3.otherwise", "the error points at nothing specific"),
        ),
    ),
    ProcedurePreset(
        library_key="request_a_callback",
        name="Request a callback",
        base_key="book_a_demo",
        trigger_text=(
            "when someone asks to be called back, or wants to speak to someone without "
            "picking a time themselves"
        ),
        installed_by=("sales-enquiries",),
        dropped_step_sids=("s4", "s5", "s6"),
        appended_steps=(
            _step(
                "s4",
                "Tell them someone will call them back, and say by when if the "
                "documents give a response time. Do not offer a specific slot — there "
                "is no calendar in this flow.",
            ),
        ),
    ),
)

PRESETS_BY_KEY: Mapping[str, ProcedurePreset] = {
    item.library_key: item for item in PRESETS
}


# ----------------------------------------------- 3.13 reconciliation, as data
#
# The ten agent templates name 24 distinct procedures between them. Every one of
# those names maps to a template, to a preset, or to a cut with a correction the
# agent templates need. Kept as data rather than a comment so
# `unaccounted_referenced_names()` can assert it instead of a reader trusting it.

REFERENCED_PROCEDURES: Mapping[str, str] = {
    "Refund request": "refund_request",
    "Where is my order": "where_is_my_order",
    "Return request": "return_request",
    "Damaged item claim": "damaged_item_claim",
    "Cannot log in": "cannot_log_in",
    "Password reset": "password_reset",
    "VPN won't connect": "vpn_wont_connect",
    "New starter setup": "new_starter_setup",
    "Leave request question": "leave_request",
    "Escalate a sensitive matter": "escalate_sensitive_matter",
    "Take a booking": "take_a_booking",
    "Book a demo": "book_a_demo",
    "Qualify a new enquiry": "qualify_new_enquiry",
    "Report a security concern": "report_security_concern",
    "Urgent enquiry": "urgent_enquiry",
    "Change or cancel a booking": "change_or_cancel_booking",
    "Book a viewing": "book_a_viewing",
    "New property enquiry": "new_property_enquiry",
    "No match — capture requirements": "no_match_capture_requirements",
    "Check eligibility": "check_eligibility",
    "Integration not working": "integration_not_working",
    "Request a callback": "request_a_callback",
    "Pricing question": "",
    "Application deadline question": "",
    "Report a suspected bug": "",
}

# The three cuts, with the correction the agent template needs. A procedure adds a
# state machine; where it adds no capability over retrieval or a bare action pair,
# shipping one is worse than not shipping one.
CUT_REFERENCES: Mapping[str, str] = {
    "Pricing question": (
        "A single-turn retrieval answer. Remove from sales-enquiries' procedures "
        "array and put the pricing guidance in that agent's system prompt."
    ),
    "Application deadline question": (
        "A single-turn retrieval answer. Remove from admissions' procedures array."
    ),
    "Report a suspected bug": (
        "collect_data plus escalate_to_human with no decision point; the agent does "
        "this without an SOP. developer-support keeps integration_not_working only."
    ),
}


# --------------------------------------------------------------------- lookups


def _action_names(steps: tuple[StepJson, ...]) -> tuple[str, ...]:
    """Every action referenced by these steps, in first-appearance order.

    Reads the parsed `actions` list, not the step text. The two differ on purpose
    in one place: `escalate_sensitive_matter` step 5 names `@collect_data` and
    `@notify_slack` in order to forbid them, and they are not references.
    """
    found: list[str] = []
    for step in steps:
        for ref in step.get("actions", ()):
            name = str(ref["action_name"])
            if name not in found:
                found.append(name)
        for branch in step.get("branches", ()):
            for name in _action_names(tuple(branch["steps"])):
                if name not in found:
                    found.append(name)
    return tuple(found)


def _rewrite(
    steps: tuple[StepJson, ...],
    text_overrides: Mapping[str, str],
    condition_overrides: Mapping[str, str],
) -> tuple[StepJson, ...]:
    """Copy steps, applying a preset's text and condition overrides.

    Copies rather than mutates: the module-level templates are shared by every
    tenant that installs one, so materialising a preset must not touch them.
    """
    out: list[StepJson] = []
    for step in steps:
        fresh: StepJson = dict(step)
        sid = str(step["sid"])
        if sid in text_overrides:
            fresh["text"] = text_overrides[sid]
        if step.get("kind") == "decision":
            branches: list[BranchJson] = []
            for branch in step.get("branches", ()):
                copy: BranchJson = dict(branch)
                label = f"{sid}.{branch['label']}"
                if label in condition_overrides:
                    copy["condition_text"] = condition_overrides[label]
                copy["steps"] = list(
                    _rewrite(tuple(branch["steps"]), text_overrides, condition_overrides)
                )
                branches.append(copy)
            fresh["branches"] = branches
        else:
            fresh["actions"] = [dict(ref) for ref in step.get("actions", ())]
        out.append(fresh)
    return tuple(out)


def _materialise(item: ProcedurePreset) -> ProcedureTemplate:
    """A preset as a full template, ready to insert exactly like a gallery entry."""
    base = BY_KEY[item.base_key]
    dropped = set(item.dropped_step_sids)
    kept = tuple(step for step in base.steps if str(step["sid"]) not in dropped)
    steps = (
        _rewrite(kept, dict(item.step_text_overrides), dict(item.branch_condition_overrides))
        + item.appended_steps
    )
    redact = (
        base.redact_transcript_on_escalation
        if item.redact_transcript_on_escalation is None
        else item.redact_transcript_on_escalation
    )
    return replace(
        base,
        library_key=item.library_key,
        name=item.name,
        trigger_text=item.trigger_text,
        steps=steps,
        action_names=_action_names(steps),
        installed_by=item.installed_by,
        redact_transcript_on_escalation=redact,
    )


def entry(key: str) -> ProcedureTemplate | None:
    """One authored template by `library_key`."""
    return BY_KEY.get(key)


def preset(key: str) -> ProcedurePreset | None:
    """One preset by `library_key`, as its deltas rather than materialised."""
    return PRESETS_BY_KEY.get(key)


def installable(key: str) -> ProcedureTemplate | None:
    """Any gallery key — template or preset — as a row-shaped record."""
    found = BY_KEY.get(key)
    if found is not None:
        return found
    variant = PRESETS_BY_KEY.get(key)
    return None if variant is None else _materialise(variant)


def gallery() -> tuple[ProcedureTemplate, ...]:
    """Everything installable, templates first, then presets, in listing order."""
    return LIBRARY + tuple(_materialise(item) for item in PRESETS)


def unknown_action_references() -> tuple[str, ...]:
    """`library_key:action_name` pairs whose action the action library lacks.

    Catches the one mistake authoring templates as code invites: an `@name` that
    no shipped action answers to. Such a template can never be enabled (§1.5
    refuses to enable a procedure with an unresolved reference), so it would
    install as a permanently dead row.
    """
    gaps: list[str] = []
    for item in gallery():
        for name in item.action_names:
            if name not in action_library.BY_KEY:
                gaps.append(f"{item.library_key}:{name}")
    return tuple(gaps)


def unaccounted_referenced_names() -> tuple[str, ...]:
    """§3.13's reconciliation, as a check rather than a table.

    A referenced procedure name is accounted for if it maps to a template, to a
    preset, or to a documented cut. Anything returned here is a name an agent
    template would install and find nothing behind.
    """
    missing: list[str] = []
    for name, key in REFERENCED_PROCEDURES.items():
        if key and (key in BY_KEY or key in PRESETS_BY_KEY):
            continue
        if not key and name in CUT_REFERENCES:
            continue
        missing.append(name)
    return tuple(missing)


