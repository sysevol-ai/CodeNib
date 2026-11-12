# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Serve one thread, with the threads it is linked to, inside a token budget.

The packet is built by code alone. Its parts, in the order they are kept when
the budget is tight: status, the ask, code locations, failing CI, linked thread
headers, discussion (newest first), review comments, linked thread content.
Every part cites the address of the event it came from, so a reader can fetch
the raw event or ask later for only what followed it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .anchor import AnchorResolver, CodeAnchor, is_source_path
from .events import BODY_KINDS, FAILED_CONCLUSIONS, ThreadEvent, ThreadHeader
from .extract import Mention, clean_body, elide, failing_lines, mentions, paragraphs
from .log_store import ThreadStore

DEFAULT_BUDGET = 3000
DEFAULT_CODE_BUDGET = 1500
MAX_LINKED_THREADS = 8
_MAX_ANCHORS = 14
_MAX_IN_FLIGHT = 5
_STATE_EVENTS = {"closed": "closed", "reopened": "open", "merged": "merged"}


def count_tokens(text: str) -> int:
    try:
        import tiktoken
    except ImportError:  # pragma: no cover - optional dependency
        return (len(text) + 3) // 4
    return len(_encoding(tiktoken).encode(text, disallowed_special=()))


_ENCODING = None


def _encoding(tiktoken):
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def short(address: str) -> str:
    """``gh:owner/repo#12/comment/34`` as ``#12/comment/34``."""

    return address[address.index("#") :] if "#" in address else address


@dataclass
class ThreadView:
    """One thread as of a moment: header facts plus its events up to then."""

    header: ThreadHeader
    events: list[ThreadEvent]
    relation: str = "root"

    @property
    def number(self) -> int:
        return self.header.number

    def state(self) -> str:
        state = "open"
        for event in self.events:
            if state == "merged":
                break  # GitHub records "closed" right after "merged"
            state = _STATE_EVENTS.get(event.kind, state)
        return state

    def labels(self) -> list[str]:
        labels: dict[str, bool] = {}
        for event in self.events:
            label = event.meta.get("label")
            if event.kind == "labeled" and label:
                labels[label] = True
            elif event.kind == "unlabeled" and label:
                labels.pop(label, None)
        return list(labels)

    def latest_checks(self) -> list[ThreadEvent]:
        checks = [e for e in self.events if e.kind == "check"]
        if not checks:
            return []
        last_sha = checks[-1].meta.get("sha")
        latest: dict[str, ThreadEvent] = {}
        for event in checks:
            if event.meta.get("sha") == last_sha:
                latest[event.meta.get("name", "")] = event
        return list(latest.values())


@dataclass
class Bundle:
    root: ThreadView
    linked: list[ThreadView] = field(default_factory=list)

    @property
    def views(self) -> list[ThreadView]:
        return [self.root, *self.linked]

    def head(self) -> str:
        """Address of the newest event, the cursor for a later delta."""

        events = [e for view in self.views for e in view.events]
        return max(events, key=lambda e: (e.at, e.address)).address if events else ""


def load_bundle(
    store: ThreadStore,
    number: int,
    *,
    at: str | None = None,
    exclude: set[int] | None = None,
    subscribed: list[int] | None = None,
) -> Bundle | None:
    """The stored thread and its stored linked threads, optionally as of ``at``.

    ``subscribed`` adds threads that are followed without a link from the root,
    and keeps earlier ones when newer links would push them out.
    """

    header = store.header(number)
    if header is None:
        return None

    def view(hdr: ThreadHeader, relation: str) -> ThreadView | None:
        events = store.events(hdr.number)
        if at:
            events = [e for e in events if e.at <= at]
        return ThreadView(hdr, events, relation) if events else None

    root = view(header, "root")
    if root is None:
        return None
    bundle = Bundle(root)
    refs = linked_numbers(root.header, root.events)
    refs += [n for n in subscribed or [] if n not in refs and n != number]
    for ref in refs:
        if exclude and ref in exclude:
            continue
        linked_header = store.header(ref)
        if linked_header is None:
            continue
        relation = header.links.get(str(ref), "subscribed")
        if relation == "subscribed" and ref in linked_numbers(root.header, root.events):
            relation = "mentions"
        if linked_header.links.get(str(number)) == "closes":
            relation = "closed-by"
        linked = view(linked_header, relation)
        if linked:
            bundle.linked.append(linked)
    return bundle


def linked_numbers(header: ThreadHeader, events: list[ThreadEvent]) -> list[int]:
    """Same-repository threads tied to this one, most recently referenced first."""

    order: dict[int, str] = {}
    for ref in header.links:
        if ref.isdigit():
            order.setdefault(int(ref), "")
    for event in events:
        ref = event.meta.get("from")
        if event.kind == "cross_reference" and isinstance(ref, int):
            order[ref] = event.at
    order.pop(header.number, None)
    ranked = sorted(order, key=lambda n: order[n], reverse=True)
    return ranked[:MAX_LINKED_THREADS]


# ------------------------------------------------------------------ items ---


@dataclass
class _Item:
    section: str
    priority: float
    order: tuple
    text: str
    tokens: int = 0


class _Dedup:
    """Drops a paragraph that an earlier part of the packet already carries."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def __call__(self, text: str) -> str:
        kept = []
        for paragraph in paragraphs(text):
            norm = re.sub(r"\s+", " ", paragraph.strip().lower())
            if len(norm) >= 80:
                key = hashlib.sha1(norm.encode()).hexdigest()
                if key in self._seen:
                    continue
                self._seen.add(key)
            kept.append(paragraph.strip())
        return "\n\n".join(kept)


def _date(at: str) -> str:
    return at[:10]


def _status_line(view: ThreadView) -> str:
    kind = "PR" if view.header.is_pr else "issue"
    parts = [
        f"{view.state()} {kind}",
        f"opened {_date(view.header.created_at)} by {view.header.author}",
    ]
    labels = view.labels()
    if labels:
        parts.append("labels: " + ", ".join(labels[:6]))
    checks = view.latest_checks()
    if checks:
        failed = [
            c.meta.get("name", "?")
            for c in checks
            if c.meta.get("conclusion") in FAILED_CONCLUSIONS
        ]
        parts.append(
            f"CI: {len(failed)} failing ({', '.join(failed[:4])})"
            if failed
            else f"CI: {len(checks)} checks, none failing"
        )
    return " · ".join(parts)


def _cited_mentions(views: list[ThreadView]) -> list[tuple[str, Mention]]:
    cited: list[tuple[str, Mention]] = []
    for view in views:
        for event in view.events:
            cite = short(event.address)
            if event.kind == "review_comment" and event.meta.get("path"):
                cited.append(
                    (
                        cite,
                        Mention(
                            "review",
                            event.meta["path"],
                            int(event.meta.get("line") or 0),
                        ),
                    )
                )
            if event.kind == "diff":
                for path in event.meta.get("files", [])[:30]:
                    cited.append((cite, Mention("diff", path)))
            if event.kind in BODY_KINDS and not event.is_bot:
                text = event.meta.get("title", "") + "\n" + event.body
                cited.extend((cite, m) for m in mentions(text))
            if (
                event.kind == "check"
                and event.meta.get("conclusion") in FAILED_CONCLUSIONS
            ):
                log = failing_lines(event.meta.get("log", ""))
                cited.extend((cite, m) for m in mentions(log) if m.kind != "symbol")
    return cited


def resolve_anchors(
    bundle: Bundle, checkout: Path | None, rev: str = "HEAD"
) -> list[CodeAnchor]:
    if checkout is None:
        return []
    resolver = AnchorResolver(checkout, rev)
    return resolver.resolve(_cited_mentions(bundle.views))


def _anchor_line(anchor: CodeAnchor) -> str:
    name = f" `{anchor.symbol}`" if anchor.symbol else ""
    cites = ", ".join(anchor.cites[:3]) + (" ..." if len(anchor.cites) > 3 else "")
    return f"- {anchor.location()}{name} ({anchor.basis}; {cites})"


def _event_items(
    view: ThreadView,
    dedup: _Dedup,
    *,
    base_priority: float,
    section: str,
    skip_opened: bool,
    body_chars: int,
) -> list[_Item]:
    items: list[_Item] = []
    humans = [
        e
        for e in view.events
        if e.kind in ("comment", "review") and not e.is_bot and e.body.strip()
    ]
    if not skip_opened:
        humans = [e for e in view.events if e.kind == "opened"] + humans
    for rank, event in enumerate(reversed(humans)):
        body = dedup(clean_body(event.body))
        if not body:
            continue
        state = f" [{event.meta['state']}]" if event.meta.get("state") else ""
        head = f"[{short(event.address)}] {_date(event.at)} {event.actor}{state}:"
        items.append(
            _Item(
                section,
                base_priority + rank * 0.001,
                (view.number, event.at),
                f"{head}\n{elide(body, body_chars)}",
            )
        )
    return items


def _review_items(
    view: ThreadView, dedup: _Dedup, priority: float, body_chars: int = 500
) -> list[_Item]:
    items = []
    comments = [e for e in view.events if e.kind == "review_comment" and not e.is_bot]
    for rank, event in enumerate(reversed(comments)):
        body = dedup(clean_body(event.body))
        if not body:
            continue
        where = event.meta.get("path", "")
        if event.meta.get("line"):
            where += f":{event.meta['line']}"
        items.append(
            _Item(
                "Review comments",
                priority + rank * 0.001,
                (view.number, event.at),
                f"[{short(event.address)}] {where} {event.actor}: {elide(body, body_chars)}",
            )
        )
    return items


def _failure_signature(lines: str) -> set[str]:
    """Failing lines with run-specific detail removed, to compare two jobs."""

    signature = set()
    for line in lines.splitlines():
        line = re.sub(r"[A-Za-z]:[\\/]\S+|/\S+/", "", line)  # runner paths
        line = re.sub(r"\d+(\.\d+)?(s|ms|%)?", "N", line).strip()
        if len(line) > 12:
            signature.add(line)
    return signature


def _ci_items(view: ThreadView, priority: float) -> list[_Item]:
    """Failing checks of the latest commit; jobs that fail alike are shown once."""

    groups: list[dict] = []
    for event in view.latest_checks():
        if event.meta.get("conclusion") not in FAILED_CONCLUSIONS:
            continue
        lines = failing_lines(event.meta.get("log", ""), cap=25)
        if not lines and event.meta.get("log_missing"):
            lines = "(log no longer available)"
        signature = _failure_signature(lines)
        match = next(
            (
                g
                for g in groups
                if signature
                and len(signature & g["signature"])
                >= 0.6 * min(len(signature), len(g["signature"]))
            ),
            None,
        )
        if match:
            match["same"].append(event.meta.get("name", "?"))
            continue
        groups.append(
            {"event": event, "lines": lines, "signature": signature, "same": []}
        )
    items = []
    for group in groups:
        event = group["event"]
        sha = str(event.meta.get("sha", ""))[:8]
        head = f"[{short(event.address)}] {event.meta.get('name')} failed at {sha}"
        if group["same"]:
            head += f" (same failure in: {', '.join(group['same'])})"
        items.append(
            _Item(
                "Failing CI",
                priority,
                (view.number, event.at),
                f"{head}\n{elide(group['lines'], 1800)}".rstrip(),
            )
        )
    return items


_RELATION_LABEL = {
    "closed-by": "pull request for this issue",
    "closes": "issue this pull request closes",
    "mentions": "mentioned",
    "subscribed": "followed",
}


def _linked_header(view: ThreadView) -> str:
    """One line a reader can act on without opening the linked thread."""

    parts = [_status_line(view)]
    commits = sum(1 for e in view.events if e.kind == "commit")
    if commits:
        parts.append(f"{commits} commits")
    diffs = [e for e in view.events if e.kind == "diff"]
    if diffs:
        meta = diffs[-1].meta
        files = meta.get("files", [])
        shown = ", ".join(files[:6]) + (
            f" and {len(files) - 6} more" if files[6:] else ""
        )
        parts.append(
            f"changes +{meta.get('additions', 0)}/-{meta.get('deletions', 0)} in {shown}"
        )
    verdicts: dict[str, str] = {}
    for event in view.events:
        state = event.meta.get("state")
        if event.kind == "review" and state in ("approved", "changes_requested"):
            verdicts[event.actor] = state.replace("_", " ")
    if verdicts:
        parts.append(
            "reviews: " + ", ".join(f"{v} by {a}" for a, v in verdicts.items())
        )
    relation = _RELATION_LABEL.get(view.relation, view.relation)
    return f"- #{view.number} ({relation}) {view.header.title} — " + " · ".join(parts)


_SECTION_ORDER = [
    "Ask",
    "Code locations",
    "Code",
    "Also in progress on this code",
    "Failing CI",
    "Linked threads",
    "Discussion",
    "Review comments",
    "Linked thread content",
]


def _fit(items: list[_Item], budget: int) -> tuple[list[_Item], dict[str, int]]:
    kept: list[_Item] = []
    omitted: dict[str, int] = {}
    left = budget
    for item in sorted(items, key=lambda i: i.priority):
        item.tokens = count_tokens(item.text) + 2
        if item.tokens <= left:
            kept.append(item)
            left -= item.tokens
        else:
            omitted[item.section] = omitted.get(item.section, 0) + 1
    return kept, omitted


def _render(title: str, status: str, kept, omitted, footer: str) -> str:
    out = [title, status]
    for section in _SECTION_ORDER:
        rows = sorted((i for i in kept if i.section == section), key=lambda i: i.order)
        if rows:
            out.append(f"\n## {section}")
            out.extend(item.text for item in rows)
    if omitted:
        left_out = ", ".join(
            f"{section} {omitted[section]}"
            for section in _SECTION_ORDER
            if omitted.get(section)
        )
        out.append(f"\nOmitted for budget: {left_out}.")
    out.append(footer)
    return "\n".join(out).strip() + "\n"


@dataclass
class Packet:
    text: str
    tokens: int
    head: str
    anchors: list[CodeAnchor]
    omitted: dict[str, int]

    def to_json(self) -> dict:
        return {
            "text": self.text,
            "tokens": self.tokens,
            "head": self.head,
            "anchors": [a.to_json() for a in self.anchors],
            "omitted": self.omitted,
        }


def _since(bundle: Bundle, since: str) -> Bundle | None:
    """The bundle restricted to events after the one addressed by ``since``."""

    events = [e for view in bundle.views for e in view.events]
    wanted = short(since)
    cursor = next((e for e in events if short(e.address) == wanted), None)
    if cursor is None:
        return None
    key = (cursor.at, cursor.address)

    def after(view: ThreadView) -> ThreadView:
        kept = [e for e in view.events if (e.at, e.address) > key]
        return ThreadView(view.header, kept, view.relation)

    return Bundle(after(bundle.root), [after(v) for v in bundle.linked])


_MAX_EXCERPT_LINES = 60


def _code_items(
    anchors: list[CodeAnchor], resolver: AnchorResolver, budget: int
) -> list[_Item]:
    """Source of the symbols the thread points at, best anchors first."""

    items: list[_Item] = []
    left = budget
    seen: list[tuple[str, int, int]] = []
    for rank, anchor in enumerate(anchors):
        if not anchor.start_line or not is_source_path(anchor.path):
            continue
        start, end = anchor.start_line, anchor.end_line
        if any(p == anchor.path and s <= start and end <= e for p, s, e in seen):
            continue  # already shown inside an enclosing symbol
        shown_end = min(end, start + _MAX_EXCERPT_LINES - 1)
        source = resolver.source(anchor.path, start, shown_end)
        if not source.strip():
            continue
        note = f" (first {_MAX_EXCERPT_LINES} lines)" if shown_end < end else ""
        text = (
            f"### {anchor.path}:{start}-{end} `{anchor.symbol}`{note}\n"
            f"```\n{source}\n```"
        )
        tokens = count_tokens(text) + 2
        if tokens > left:
            continue
        left -= tokens
        seen.append((anchor.path, start, shown_end))
        items.append(_Item("Code", 2.2 + rank * 0.001, (rank,), text, tokens))
    return items


def _thread_items(bundle: Bundle, full: Bundle, delta: bool, caps: bool) -> list[_Item]:
    """Everything a packet can say about the threads, before the budget cut."""

    def cap(chars: int) -> int:
        return chars if caps else 10**9

    root = bundle.root
    dedup = _Dedup()
    items: list[_Item] = []
    if not delta:
        opened = next((e for e in root.events if e.kind == "opened"), None)
        if opened:
            body = dedup(clean_body(opened.body))
            items.append(
                _Item(
                    "Ask",
                    1.0,
                    (0, ""),
                    f"[{short(opened.address)}] {root.header.title}\n"
                    f"{elide(body, cap(3200))}",
                )
            )
    items += _ci_items(root, 2.5)
    for view in bundle.linked:
        if view.events:
            shown = ThreadView(view.header, _all(full, view), view.relation)
            items.append(
                _Item("Linked threads", 3.0, (view.number,), _linked_header(shown))
            )
    items += _event_items(
        root,
        dedup,
        base_priority=4.0,
        section="Discussion",
        skip_opened=True,
        body_chars=cap(1400),
    )
    items += _review_items(root, dedup, 5.0, cap(500))
    for view in bundle.linked:
        items += _ci_items(view, 5.5)
        items += _event_items(
            view,
            dedup,
            base_priority=6.0,
            section="Linked thread content",
            skip_opened=False,
            body_chars=cap(700),
        )
        items += _review_items(view, dedup, 7.0, cap(500))
    return items


_ELIDED = " chars omitted ...]"


def _coverage(bundle: Bundle, kept: list[_Item], omitted: dict[str, int]) -> str:
    """Say whether the packet replaces reading the threads, or only starts it."""

    written = [
        e
        for view in bundle.views
        for e in view.events
        if e.kind in BODY_KINDS and e.body.strip()
    ]
    people = sum(1 for e in written if not e.is_bot)
    bots = len(written) - people
    spoken = ("Ask", "Discussion", "Review comments", "Linked thread content")
    shortened = sum(1 for i in kept if i.section in spoken and _ELIDED in i.text)
    dropped = sum(omitted.get(section, 0) for section in spoken)
    left_out = f"left out: bot comments ({bots}), quoted replies, passing CI"
    if not shortened and not dropped:
        return (
            f"Coverage: complete. Everything people wrote in these threads "
            f"({people} posts) is below in full; {left_out}."
        )
    return (
        f"Coverage: partial. Of {people} posts by people, {shortened} are "
        f"shortened and {dropped} omitted; {left_out}."
    )


def build_packet(
    bundle: Bundle,
    *,
    budget: int = DEFAULT_BUDGET,
    code_budget: int = DEFAULT_CODE_BUDGET,
    since: str | None = None,
    checkout: Path | None = None,
    rev: str = "HEAD",
    open_pulls: list | None = None,
) -> Packet:
    """Project a bundle into ``budget`` tokens, or only what followed ``since``.

    ``code_budget`` is spent on top of ``budget``, on the source of the symbols
    the thread points at, and only when a checkout is given. ``open_pulls`` is
    the index of open pull requests; those that change the same code are named.
    """

    full = bundle
    delta = since is not None
    if delta:
        restricted = _since(bundle, since or "")
        if restricted is None:
            raise KeyError(f"no event {since!r} in this thread or its linked threads")
        bundle = restricted

    root = bundle.root
    repo = full.root.header.repo
    anchors: list[CodeAnchor] = []
    anchor_items: list[_Item] = []
    code_items: list[_Item] = []
    if checkout is not None:
        resolver = AnchorResolver(checkout, rev)
        anchors = resolver.resolve(_cited_mentions(bundle.views))
        for rank, anchor in enumerate(anchors[:_MAX_ANCHORS]):
            anchor_items.append(
                _Item(
                    "Code locations", 2.0 + rank * 0.001, (rank,), _anchor_line(anchor)
                )
            )
        if code_budget > 0:
            code_items = _code_items(anchors[:_MAX_ANCHORS], resolver, code_budget)
        if open_pulls and not delta:
            from .in_flight import match_in_flight

            known = {view.number for view in full.views}
            matches = match_in_flight(anchors, open_pulls, known)
            for rank, match in enumerate(matches[:_MAX_IN_FLIGHT]):
                anchor_items.append(
                    _Item(
                        "Also in progress on this code",
                        2.3 + rank * 0.001,
                        (rank,),
                        match.line(),
                    )
                )

    kind = "PR" if root.header.is_pr else "issue"
    if delta:
        title = f"# Thread update: {repo}#{root.number} since {short(since or '')}"
        changed = sum(len(v.events) for v in bundle.views)
        status = f"Status now: {_status_line(full.root)} · {changed} new events"
    else:
        title = f"# Thread context: {repo}#{root.number} ({kind})"
        events = sum(len(v.events) for v in bundle.views)
        status = (
            f"Status: {_status_line(root)} · {events} events across "
            f"{len(bundle.views)} threads"
        )
    head = full.head()
    footer = (
        f"\nCursor: {short(head)}. Bracketed ids cite thread events; fetch one "
        f"with `codenib threads events <id>`, or later changes with "
        f"`codenib threads show {root.number} --since {short(head)}`."
    )
    coverage_room = 45
    fixed = count_tokens(title + status + footer) + coverage_room
    code_tokens = sum(item.tokens for item in code_items)
    limit = budget + code_tokens

    # First try to carry everything people wrote, unshortened. Only when that
    # does not fit are long posts cut to their head and tail.
    for caps in (False, True):
        items = anchor_items + _thread_items(bundle, full, delta, caps)
        kept, omitted = _fit(items, max(0, budget - fixed))
        if not omitted:
            break
    kept += code_items

    def render() -> str:
        line = f"{status}\n{_coverage(bundle, kept, omitted)}"
        return _render(title, line, kept, omitted, footer)

    text = render()
    # Section headings and the omission note are not items, so a tight budget
    # can still be exceeded. Give up the least important item until it fits.
    while kept and count_tokens(text) > limit:
        dropped = max(kept, key=lambda item: item.priority)
        kept.remove(dropped)
        omitted[dropped.section] = omitted.get(dropped.section, 0) + 1
        text = render()
    return Packet(text, count_tokens(text), head, anchors, omitted)


def _all(full: Bundle, view: ThreadView) -> list[ThreadEvent]:
    for candidate in full.views:
        if candidate.number == view.number:
            return candidate.events
    return view.events


def render_raw(bundle: Bundle) -> str:
    """Every event of the bundle with nothing removed. A baseline, not a product."""

    out = []
    for view in bundle.views:
        out.append(f"# {bundle.root.header.repo}#{view.number} {view.header.title}")
        for event in view.events:
            meta = {k: v for k, v in event.meta.items() if k not in ("log", "hunk")}
            out.append(f"[{event.at}] {event.kind} by {event.actor} {meta or ''}")
            for part in (event.meta.get("hunk"), event.body, event.meta.get("log")):
                if part:
                    out.append(part)
    return "\n".join(out) + "\n"


__all__ = [
    "Bundle",
    "DEFAULT_BUDGET",
    "DEFAULT_CODE_BUDGET",
    "Packet",
    "ThreadView",
    "build_packet",
    "count_tokens",
    "linked_numbers",
    "load_bundle",
    "render_raw",
    "resolve_anchors",
    "short",
]
