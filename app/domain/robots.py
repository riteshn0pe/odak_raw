"""`robots.txt` parsing. Pure functions, no I/O — the fetch belongs to the crawl
fetch service, which is the only place allowed to open a socket to a tenant URL.

Hand written rather than `urllib.robotparser` because that class fetches
synchronously, does not expose `Sitemap:` directives at all, and its
`Crawl-delay` support differs between Python versions. All three are things the
crawler depends on, so the ~40 lines of matching logic are cheaper than working
around the stdlib.

What is honoured, per the de-facto standard and RFC 9309:

* The most specific matching `User-agent` group — our own token first, then `*`.
  A file that names us takes precedence over the wildcard group even when the
  wildcard group appears later.
* Longest-match wins between `Allow` and `Disallow`, with `*` and `$` wildcards.
  On an equal-length tie `Allow` wins, which is what Google's implementation does
  and what site owners write their files expecting.
* `Crawl-delay`, clamped into `[CRAWL_MIN_DELAY_MS, CRAWL_MAX_DELAY_MS]`. A site
  asking for 120 s would hold a 500 page crawl for 17 hours, which is worse for
  the tenant than a slightly impolite crawl, so the ceiling wins and
  `crawl_delay_clamped` says so for the log line.
* Every `Sitemap:` directive, which is free discovery and better than link
  following because it is the site's own list of canonical URLs plus `<lastmod>`.

A missing, 4xx or 5xx `robots.txt` means "no restrictions" — that is
:meth:`RobotsTxt.permissive`, not an empty rule set that happens to allow
everything, so the distinction is visible to a reader.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from app.core.constants import CRAWL_MAX_DELAY_MS, CRAWL_MIN_DELAY_MS

# a rule line is `field: value`; the colon is required, everything after the
# first one is the value, and a trailing `# comment` is stripped
_LINE_RE = re.compile(r"^\s*([A-Za-z-]+)\s*:\s*(.*?)\s*$")


def _strip_comment(value: str) -> str:
    index = value.find("#")
    return value[:index].strip() if index >= 0 else value.strip()


def _rule_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a robots path pattern to a regex anchored at the path start.

    Only `*` and `$` are wildcards in robots.txt; every other regex
    metacharacter in a path (`.`, `+`, `(`) is a literal, so the pattern is
    escaped piecewise rather than passed through.
    """
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    parts = [re.escape(piece) for piece in body.split("*")]
    expression = ".*".join(parts)
    return re.compile("^" + expression + ("$" if anchored_end else ""))


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str
    matcher: re.Pattern[str]

    @property
    def length(self) -> int:
        """Specificity. The raw pattern length is what the standard compares, so
        `/a/b/*` beats `/a/` even though both match `/a/b/c`."""
        return len(self.pattern)


@dataclass(slots=True)
class RobotsTxt:
    """Parsed rules for one origin, already narrowed to our user-agent group.

    `restrictive` is False for the "no usable robots.txt" case. Keeping it as a
    field rather than inferring it from an empty rule list means a file that
    genuinely contains `Disallow:` (i.e. allow everything) is not confused with a
    404, which matters because only the first case should suppress the sitemap
    probe fallback logging.
    """

    rules: tuple[_Rule, ...] = ()
    sitemaps: tuple[str, ...] = ()
    crawl_delay_ms: int | None = None
    crawl_delay_clamped: bool = False
    restrictive: bool = True
    # every group seen, kept for the "the file named us specifically" log line
    matched_agent: str = "*"
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def permissive(cls) -> RobotsTxt:
        """No restrictions. A missing, 4xx or 5xx robots.txt lands here."""
        return cls(restrictive=False)

    @classmethod
    def parse(cls, text: str, user_agent: str = "*") -> RobotsTxt:
        """Parse a robots.txt body for `user_agent`.

        A file we cannot make sense of yields a permissive result rather than an
        error: refusing to crawl because a site's robots.txt is malformed is a
        worse outcome for the tenant than crawling it, and the same-origin plus
        urlguard controls do not depend on this file.
        """
        token = _agent_token(user_agent)

        # groups keyed by lower-cased agent token. consecutive User-agent lines
        # share one group, which is the shape most real files use
        groups: dict[str, list[tuple[str, str]]] = {}
        delays: dict[str, str] = {}
        sitemaps: list[str] = []
        current: list[str] = []
        expecting_agent = True

        for raw_line in text.splitlines():
            match = _LINE_RE.match(raw_line)
            if match is None:
                continue
            field_name = match.group(1).lower()
            value = _strip_comment(match.group(2))

            if field_name == "sitemap":
                if value and value not in sitemaps:
                    sitemaps.append(value)
                continue

            if field_name == "user-agent":
                if not expecting_agent:
                    current = []
                    expecting_agent = True
                agent = value.lower()
                if agent:
                    current.append(agent)
                    groups.setdefault(agent, [])
                continue

            if field_name in ("allow", "disallow"):
                expecting_agent = False
                for agent in current:
                    groups[agent].append((field_name, value))
                continue

            if field_name == "crawl-delay":
                expecting_agent = False
                for agent in current:
                    delays[agent] = value
                continue

            # request-rate, visit-time, host, clean-param and anything else are
            # not part of the standard we implement and are ignored silently
            expecting_agent = False

        agent_key = _select_group(token, groups)
        if agent_key is None:
            return cls(sitemaps=tuple(sitemaps), restrictive=False)

        rules: list[_Rule] = []
        for kind, value in groups[agent_key]:
            if not value:
                # `Disallow:` with an empty value means "allow everything" and
                # carries no pattern; `Allow:` empty is meaningless. Neither
                # produces a rule
                continue
            normalised = value if value.startswith("/") else "/" + value
            rules.append(_Rule(kind == "allow", normalised, _rule_to_regex(normalised)))

        delay_ms, clamped = _parse_delay(delays.get(agent_key))

        return cls(
            rules=tuple(rules),
            sitemaps=tuple(sitemaps),
            crawl_delay_ms=delay_ms,
            crawl_delay_clamped=clamped,
            restrictive=True,
            matched_agent=agent_key,
        )

    def allows(self, path_or_url: str) -> bool:
        """True when our agent may fetch this path.

        Accepts a full URL or a bare path so callers do not have to split it
        twice. Percent-escapes are decoded once before matching, because a site
        that writes `Disallow: /private` must also stop `/%70rivate`.
        """
        if not self.rules:
            return True

        path = _path_of(path_or_url)
        best: _Rule | None = None
        for rule in self.rules:
            if not rule.matcher.match(path):
                continue
            if best is None:
                best = rule
                continue
            if rule.length > best.length or (
                # equal specificity: allow wins, matching Google's behaviour and
                # what site owners write their files expecting
                rule.length == best.length
                and rule.allow
                and not best.allow
            ):
                best = rule
        if best is None:
            return True
        return best.allow


def _path_of(path_or_url: str) -> str:
    if "://" in path_or_url:
        parts = urlsplit(path_or_url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
    else:
        path = path_or_url or "/"
    try:
        return unquote(path) or "/"
    except (UnicodeDecodeError, ValueError):
        return path or "/"


def _agent_token(user_agent: str) -> str:
    """The product token out of a full User-Agent string.

    `ContextAgentBot/1.0 (+https://…/bot)` matches a `User-agent: contextagentbot`
    group, which is how site owners write the name.
    """
    head = user_agent.strip().split()[0] if user_agent.strip() else "*"
    return head.split("/", 1)[0].lower() or "*"


def _select_group(token: str, groups: dict[str, list[tuple[str, str]]]) -> str | None:
    """The group that applies to us: our token, then the longest prefix of it that
    a group names, then `*`. None when the file names neither."""
    if token in groups:
        return token
    # a file may name `contextagent` while our token is `contextagentbot`; the
    # longest naming prefix wins, which is the substring rule from the standard
    candidates = [agent for agent in groups if agent != "*" and token.startswith(agent)]
    if candidates:
        return max(candidates, key=len)
    if "*" in groups:
        return "*"
    return None


def _parse_delay(raw: str | None) -> tuple[int | None, bool]:
    """`Crawl-delay` in milliseconds, clamped, plus whether the clamp bit."""
    if raw is None:
        return None, False
    try:
        seconds = float(raw)
    except ValueError:
        return None, False
    if not math.isfinite(seconds) or seconds <= 0:
        # `Crawl-delay: nan` / `1e999` would raise ValueError/OverflowError in
        # int(), and this parser is documented never to raise
        return None, False
    requested = int(seconds * 1000)
    clamped = max(CRAWL_MIN_DELAY_MS, min(requested, CRAWL_MAX_DELAY_MS))
    return clamped, clamped != requested
