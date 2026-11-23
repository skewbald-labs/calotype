"""Key administration.

    python -m calotype.admin create-key --plan free --label "first user"
    python -m calotype.admin list-keys
    python -m calotype.admin revoke-key 3 [--at-period-end] [--no-stripe]
    python -m calotype.admin delete-key 3 [--yes] [--at-period-end] [--no-stripe]
    python -m calotype.admin key-id < file-holding-the-key
    python -m calotype.admin mark-internal 1
    python -m calotype.admin set-plan 3 starter
    python -m calotype.admin usage 3
    python -m calotype.admin contacts
    python -m calotype.admin credits 3
    python -m calotype.admin refund-credit pi_... [--yes] [--prorated] [--no-stripe]
    python -m calotype.admin remove-email 3            (or an address: every key that holds it)
    python -m calotype.admin tries [--days 7]          (the home page's free tries, per UTC day)
    python -m calotype.admin funnel [--days 7]         (steps toward a key, per UTC day; see funnel.py)

Keys made here are marked internal and never count as demand-test contacts.
``mark-internal`` does the same for a test key made through the website's
public button, such as the operator's own first key.

The new key is printed once and never stored; only its SHA-256 hash is kept.

Revoking a paid key cancels its Stripe subscription first, at once and with no
prorated refund, or with ``--at-period-end`` at the end of the month already
paid for. That needs ``STRIPE_SECRET_KEY`` in the environment. Without it the
command refuses, unless ``--no-stripe`` is given: then the key is revoked and
the subscription has to be cancelled by hand in the Stripe dashboard.

``delete-key`` is for a key holder who asks for their record to be deleted
(see the privacy notice). It deletes the key's record (with its email address,
if it has one), its usage counts and its quota notices, for good, after cancelling a paid key's subscription as ``revoke-key`` does.
Without ``--yes`` it only prints what it would delete. ``key-id`` reads a key
from standard input, so it stays out of the shell history, and prints its id.
Revoking a key also removes its email address.

``remove-email`` is for a holder who replies "stop" to a quota notice: given
an address, it removes it from every key that holds it; given a key id, from
that key. It never prints the address.

``tries`` prints the daily counts of the home page's free tries, newest day
first: every try, those rendered (the count the daily cap applies to), and how
they ended. The counts are all there is: no URL, host or IP address is stored.

``funnel`` prints the daily funnel counts (``funnel.py``), newest day first,
with a total row: agents that looked at the MCP tool or called it without a
key, wrong keys, OAuth probes, misses on key and sign-up paths, help pages
shown to browsers on API-only addresses (``api_page``), refused and made keys,
keys made with the site's button (``web_key``, also in ``key_made``), and first
screenshots. Counts only, like ``tries``.

``credits`` shows a key's prepaid credit and its purchases. ``refund-credit``
undoes one purchase, named by its Stripe PaymentIntent id: it takes back the
credit that is still unused (the pack's size or the key's whole balance,
whichever is smaller; screenshots already taken stay taken) and refunds the
payment through Stripe, in full, or with ``--prorated`` only the share that
was unused. Without ``--yes`` it only prints what it would do. It needs
``STRIPE_SECRET_KEY``; with ``--no-stripe`` it only takes the credit back, and
the money has to be refunded by hand in the Stripe dashboard. Running it again
after a failure finishes the job and never refunds twice.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from .billing import ALREADY_ENDED, CANCELED, CANCELS_AT_PERIOD_END, BillingProviderError, stripe_for_admin
from .config import Settings
from .credits import credits_for_admin
from .notices import normalize_email
from .storage import (
    FUNNEL_EVENTS,
    ApiKey,
    CreditPurchase,
    Storage,
    TryDay,
    current_day,
    current_month,
    open_configured_storage,
)

# Replaced in tests with a provider over a fake Stripe client.
open_billing = stripe_for_admin
open_credits = credits_for_admin

_CANCEL_MESSAGES = {
    CANCELED: "Cancelled Stripe subscription {sub} now. It will not be charged again; no prorated refund was made.",
    CANCELS_AT_PERIOD_END: "Stripe subscription {sub} will end at the end of the month already paid for "
    "and will not be charged again.",
    ALREADY_ENDED: "Stripe subscription {sub} had already ended; nothing to cancel.",
}


def end_subscription(key: ApiKey, *, at_period_end: bool, no_stripe: bool, action: str) -> bool:
    """Cancel a paid key's Stripe subscription before ``action`` ("revoke", "delete").

    Returns True if ``action`` may go ahead: the key has no subscription, the
    subscription was cancelled, or ``no_stripe`` was given (with a warning).
    Returns False, having said why, if it must not.
    """
    sub = key.stripe_subscription_id
    if not sub:
        return True
    if no_stripe:
        print(
            f"WARNING: --no-stripe: Stripe subscription {sub} of key {key.id} was NOT cancelled. "
            "Cancel it by hand in the Stripe dashboard, or it will keep charging.",
            file=sys.stderr,
        )
        return True
    provider = open_billing()
    if provider is None:
        print(
            f"Key {key.id} has Stripe subscription {sub}, and STRIPE_SECRET_KEY is not set, so it cannot be "
            f"cancelled from here. Nothing was changed. Set STRIPE_SECRET_KEY and run again, or pass --no-stripe "
            f"to {action} the key anyway and cancel the subscription by hand.",
            file=sys.stderr,
        )
        return False
    try:
        outcome = provider.cancel_subscription(sub, at_period_end=at_period_end)
    except BillingProviderError as e:
        print(
            f"Stripe could not cancel subscription {sub} ({e}). Nothing was changed. Try again, or pass "
            f"--no-stripe to {action} the key anyway and cancel the subscription by hand.",
            file=sys.stderr,
        )
        return False
    print(_CANCEL_MESSAGES[outcome].format(sub=sub), file=sys.stderr)
    return True


STRIPE_RECORDS_NOTE = (
    "Note: Stripe keeps its own records of this customer's payments (invoices, receipts and the "
    "billing details given at checkout), as the law requires of payment records. They are not "
    "deleted here; Calotype no longer holds the Stripe ids that link them to this key."
)


def describe_key(key: ApiKey) -> str:
    state = "revoked" if key.revoked else "active"
    source = "internal" if key.internal else "self-serve"
    return (
        f"key {key.id}: plan={key.plan} state={state} source={source} created_at={key.created_at} "
        f"screenshots={key.successful_screenshots} label={key.label!r}"
    )


def describe_purchase(p: CreditPurchase) -> str:
    state = "paid"
    if p.refunded_at:
        how = f"Stripe refund {p.refund_id}" if p.refund_id else "no Stripe refund recorded"
        state = f"refunded {p.refunded_at} ({p.credits_removed} credits taken back; {how})"
    return (
        f"{p.id}: key={p.key_id} pack={p.pack} screenshots={p.screenshots} "
        f"paid=${p.amount_cents / 100:.2f} {p.currency} at={p.created_at} {state}"
    )


def refund_credit(store: Storage, purchase_id: str, *, yes: bool, prorated: bool, no_stripe: bool) -> int:
    """Undo one prepaid purchase: take back its unused credit, then refund it through Stripe."""
    purchase = store.get_credit_purchase(purchase_id)
    if purchase is None:
        print("no credit purchase with that PaymentIntent id", file=sys.stderr)
        return 1
    if purchase.refund_id:
        print(f"already refunded: {describe_purchase(purchase)}", file=sys.stderr)
        return 0

    def refund_cents(removed: int) -> int:
        return purchase.amount_cents * removed // purchase.screenshots if prorated else purchase.amount_cents

    if not yes:
        balance = store.get_credit_balance(purchase.key_id)
        removed = purchase.credits_removed if purchase.refunded_at else min(purchase.screenshots, balance)
        print(f"Would refund {describe_purchase(purchase)}", file=sys.stderr)
        if purchase.refunded_at:
            print(f"  its {removed} unused credits were already taken back", file=sys.stderr)
        else:
            print(
                f"  take back {removed} of key {purchase.key_id}'s {balance} credits "
                f"({purchase.screenshots - removed} of this pack are already used)",
                file=sys.stderr,
            )
        how = "NOT through Stripe (--no-stripe): refund it by hand" if no_stripe else "through Stripe"
        print(
            f"  refund ${refund_cents(removed) / 100:.2f} of ${purchase.amount_cents / 100:.2f} {how}", file=sys.stderr
        )
        print("Run again with --yes to do it.", file=sys.stderr)
        return 0

    provider = None
    if not no_stripe:
        provider = open_credits()
        if provider is None:
            print(
                "STRIPE_SECRET_KEY is not set, so the payment cannot be refunded from here. Nothing was changed. "
                "Set it and run again, or pass --no-stripe to take the credit back and refund by hand.",
                file=sys.stderr,
            )
            return 1
    purchase, changed = store.refund_credit_purchase(purchase_id)
    if changed:
        print(f"took back {purchase.credits_removed} unused credits from key {purchase.key_id}", file=sys.stderr)
    else:
        print(f"the {purchase.credits_removed} unused credits had already been taken back", file=sys.stderr)
    amount = refund_cents(purchase.credits_removed)
    if provider is None:
        print(
            f"WARNING: --no-stripe: PaymentIntent {purchase.id} was NOT refunded. "
            f"Refund ${amount / 100:.2f} by hand in the Stripe dashboard.",
            file=sys.stderr,
        )
        return 0
    if amount <= 0:
        print("nothing of this pack was unused, so the prorated refund is $0.00; no refund was made", file=sys.stderr)
        return 0
    try:
        refund_id = provider.refund(purchase, amount)
    except BillingProviderError as e:
        print(
            f"Stripe could not refund PaymentIntent {purchase.id} ({e}). The credit was taken back; the money was "
            "not returned. Run the same command again, or refund it by hand in the Stripe dashboard.",
            file=sys.stderr,
        )
        return 1
    store.set_credit_refund_id(purchase.id, refund_id)
    print(f"refunded ${amount / 100:.2f} through Stripe: {refund_id}", file=sys.stderr)
    return 0


def _add_stripe_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--at-period-end",
        action="store_true",
        help="for a paid key, end the subscription at the end of the paid month instead of now",
    )
    p.add_argument(
        "--no-stripe",
        action="store_true",
        help="do not touch Stripe; the subscription must then be cancelled by hand",
    )


def _positive_int(text: str) -> int:
    n = int(text)
    if not 1 <= n <= 366:
        raise argparse.ArgumentTypeError("give a number of days from 1 to 366")
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m calotype.admin", description="Manage Calotype API keys.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-key", help="create a key and print it once")
    c.add_argument("--plan", required=True)
    c.add_argument("--label", default="")

    sub.add_parser("list-keys", help="list keys (prefix, plan, label; never the key)")

    r = sub.add_parser("revoke-key", help="revoke a key by id; a paid key's subscription is cancelled first")
    r.add_argument("key_id", type=int)
    _add_stripe_flags(r)

    d = sub.add_parser(
        "delete-key", help="delete a key's record and usage counts, on the holder's request (dry run without --yes)"
    )
    d.add_argument("key_id", type=int)
    d.add_argument("--yes", action="store_true", help="really delete; without it, only show what would be deleted")
    _add_stripe_flags(d)

    sub.add_parser("key-id", help="read a key from standard input and print its id")
    f = sub.add_parser("find-ref", help="find a key by the key_ref its holder quotes (from /v1/usage)")
    f.add_argument("ref")

    m = sub.add_parser("mark-internal", help="mark a key as Calotype's own, so it never counts as a contact")
    m.add_argument("key_id", type=int)

    s = sub.add_parser("set-plan", help="move a key to another plan")
    s.add_argument("key_id", type=int)
    s.add_argument("plan")

    u = sub.add_parser("usage", help="show this month's usage for a key")
    u.add_argument("key_id", type=int)

    sub.add_parser("contacts", help="non-internal keys with a successful screenshot, by contact ordinal")

    cr = sub.add_parser("credits", help="show a key's prepaid credit and its purchases")
    cr.add_argument("key_id", type=int)

    rc = sub.add_parser(
        "refund-credit",
        help="refund a prepaid purchase and take back its unused credit (dry run without --yes)",
    )
    rc.add_argument("purchase_id", help="the purchase's Stripe PaymentIntent id, pi_...")
    rc.add_argument("--yes", action="store_true", help="really refund; without it, only show what would happen")
    rc.add_argument("--prorated", action="store_true", help="refund only the unused share of the price, not all of it")
    rc.add_argument(
        "--no-stripe", action="store_true", help="do not touch Stripe; the money must then be refunded by hand"
    )

    e = sub.add_parser("remove-email", help="remove the quota-notice address from a key id, or from every key with an address")
    e.add_argument("target", help="a key id, or an email address")

    t = sub.add_parser("tries", help="daily counts of the home page's free tries, newest first")
    t.add_argument("--days", type=_positive_int, default=7, help="how many UTC days to show, today included (default 7)")

    fu = sub.add_parser("funnel", help="daily counts of the steps toward a key (funnel.py), newest first")
    fu.add_argument("--days", type=_positive_int, default=7, help="how many UTC days to show, today included (default 7)")

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    store = open_configured_storage(settings)

    if args.cmd == "create-key":
        if args.plan not in settings.plans:
            print(f"unknown plan {args.plan!r}; plans: {', '.join(settings.plans)}", file=sys.stderr)
            return 2
        raw, key = store.create_key(args.plan, args.label, internal=True)
        print(f"Created internal key id={key.id} plan={key.plan} label={key.label!r}", file=sys.stderr)
        print("This is the only time the key is shown. Store it now.", file=sys.stderr)
        print(raw)
        return 0

    if args.cmd == "list-keys":
        print("id\tplan\tstate\tsource\tcreated_at\tscreenshots\tlabel")
        for k in store.list_keys():
            state = "revoked" if k.revoked else "active"
            source = "internal" if k.internal else "self-serve"
            print(f"{k.id}\t{k.plan}\t{state}\t{source}\t{k.created_at}\t{k.successful_screenshots}\t{k.label}")
        return 0

    if args.cmd == "contacts":
        print("ordinal\tkey_id\tplan\tcreated_at\tscreenshots\tsource")
        for c in store.list_contacts():
            print(f"{c.ordinal}\t{c.key_id}\t{c.plan}\t{c.created_at}\t{c.successful_screenshots}\t{c.source or '-'}")
        return 0

    if args.cmd == "revoke-key":
        key = store.get_key_by_id(args.key_id)
        if key is None:
            print("no key with that id", file=sys.stderr)
            return 1
        if key.revoked and (not key.stripe_subscription_id or args.no_stripe):
            print("no active key with that id (already revoked)", file=sys.stderr)
            return 1
        # A revoked key that still has a subscription goes through again, so a
        # cancellation skipped with --no-stripe or refused by Stripe can be retried.
        if not end_subscription(key, at_period_end=args.at_period_end, no_stripe=args.no_stripe, action="revoke"):
            return 1
        if store.revoke_key(key.id):
            print("revoked", file=sys.stderr)
        else:
            print("the key was already revoked", file=sys.stderr)
        return 0

    if args.cmd == "delete-key":
        key = store.get_key_by_id(args.key_id)
        if key is None:
            print("no key with that id", file=sys.stderr)
            return 1
        sub_id = key.stripe_subscription_id
        if not args.yes:
            print("Would delete, for good:", file=sys.stderr)
            print(f"  the record of {describe_key(key)}", file=sys.stderr)
            print("  all of its monthly usage counts and quota notices", file=sys.stderr)
            if key.email:
                print("  its email address for quota notices", file=sys.stderr)
            credit, purchases = store.get_credit_balance(key.id), store.list_credit_purchases(key.id)
            if credit or purchases:
                print(
                    f"  its {credit} unused prepaid credits and {len(purchases)} purchase records "
                    "(nothing is refunded; use refund-credit first if money is owed)",
                    file=sys.stderr,
                )
            if sub_id:
                when = "at the end of the paid month" if args.at_period_end else "now, with no prorated refund"
                how = "NOT (--no-stripe)" if args.no_stripe else when
                print(f"after cancelling its Stripe subscription {sub_id}: {how}.", file=sys.stderr)
            print("Run again with --yes to do it.", file=sys.stderr)
            return 0
        if not end_subscription(key, at_period_end=args.at_period_end, no_stripe=args.no_stripe, action="delete"):
            return 1
        store.delete_key(key.id)
        print(f"deleted key {key.id}, its usage counts, its notices and any email address", file=sys.stderr)
        if key.stripe_customer_id or sub_id:
            print(STRIPE_RECORDS_NOTE, file=sys.stderr)
        return 0

    if args.cmd == "key-id":
        raw = sys.stdin.readline().strip()
        key = store.get_key(raw) if raw else None
        if key is None:
            print("no key matches", file=sys.stderr)
            return 1
        print(key.id)
        return 0

    if args.cmd == "find-ref":
        key = store.find_key_by_ref(args.ref.strip().lower())
        if key is None:
            print("no single key has that reference", file=sys.stderr)
            return 1
        print(describe_key(key))
        return 0

    if args.cmd == "mark-internal":
        key = store.get_key_by_id(args.key_id)
        if key is None:
            print("no key with that id", file=sys.stderr)
            return 1
        if key.internal:
            print(f"key {key.id} is already internal", file=sys.stderr)
            return 0
        store.set_internal(key.id, True)
        print(f"key {key.id} is now internal and no longer counts as a demand-test contact", file=sys.stderr)
        return 0

    if args.cmd == "set-plan":
        if args.plan not in settings.plans:
            print(f"unknown plan {args.plan!r}", file=sys.stderr)
            return 2
        ok = store.set_plan(args.key_id, args.plan)
        print("updated" if ok else "no key with that id", file=sys.stderr)
        return 0 if ok else 1

    if args.cmd == "remove-email":
        target = args.target.strip()
        if "@" in target:
            email = normalize_email(target)
            ids = store.keys_with_email(email) if email else []
        elif target.isdigit():
            key = store.get_key_by_id(int(target))
            ids = [key.id] if key is not None and key.email else []
        else:
            print("give a key id or an email address", file=sys.stderr)
            return 2
        for key_id in ids:
            store.set_email(key_id, None)
        if not ids:
            print("no key holds that address" if "@" in target else "that key has no address", file=sys.stderr)
            return 1
        print(f"removed the address from key(s) {', '.join(map(str, ids))}", file=sys.stderr)
        return 0

    if args.cmd == "tries":
        today = datetime.now(UTC)
        days = [current_day(today - timedelta(days=n)) for n in range(args.days)]
        names = [f for f in TryDay.__dataclass_fields__ if f != "day"]
        print("day\t" + "\t".join(names))
        for d in store.try_days(days):
            print(d.day + "\t" + "\t".join(str(getattr(d, n)) for n in names))
        return 0

    if args.cmd == "funnel":
        today = datetime.now(UTC)
        days = [current_day(today - timedelta(days=n)) for n in range(args.days)]
        rows = store.funnel_days(days)
        print("day\t" + "\t".join(FUNNEL_EVENTS))
        for d in rows:
            print(d.day + "\t" + "\t".join(str(getattr(d, e)) for e in FUNNEL_EVENTS))
        print("total\t" + "\t".join(str(sum(getattr(d, e) for d in rows)) for e in FUNNEL_EVENTS))
        return 0

    if args.cmd == "usage":
        month = current_month()
        print(f"{month}\t{store.get_usage(args.key_id, month)}")
        return 0

    if args.cmd == "credits":
        if store.get_key_by_id(args.key_id) is None:
            print("no key with that id", file=sys.stderr)
            return 1
        print(f"credits\t{store.get_credit_balance(args.key_id)}")
        for purchase in store.list_credit_purchases(args.key_id):
            print(describe_purchase(purchase))
        return 0

    if args.cmd == "refund-credit":
        return refund_credit(
            store, args.purchase_id.strip(), yes=args.yes, prorated=args.prorated, no_stripe=args.no_stripe
        )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
