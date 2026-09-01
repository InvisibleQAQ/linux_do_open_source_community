"""GitHub repository URL extraction and canonicalization.

A pure module: no network, no database, no runtime bindings. It is the foundation
of the anti-hallucination rule — the LLM may only choose from the candidates this
module extracts from the post's own text, so anything wrong here becomes wrong
data downstream.

Two responsibilities, deliberately kept separate:

  extract_repository_candidates(text) -> list[RepositoryCandidate]
      Find every explicit GitHub repository URL in a chunk of text.

  canonicalize(url) -> RepositoryCandidate | None
      Decide whether one URL is a repository and, if so, what its identity is.

Canonical form is lowercase: GitHub owner and repository names are
case-insensitive for lookup, so `github.com/Owner/Repo` and
`github.com/owner/repo` are the same repository and must collapse onto one
`projects` row. The original URL is preserved verbatim as `evidence_url` — the
PRD requires the raw link to remain checkable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "RepositoryCandidate",
    "canonicalize",
    "extract_repository_candidates",
]

# ----------------------------------------------------------------------
# Host rules
# ----------------------------------------------------------------------

# Exact hosts only. This is an allowlist because the failure mode of a substring
# check is accepting `github.com.evil.example` or `notgithub.com`.
#
# Deliberately excluded:
#   gist.github.com          — gists are not repositories (PRD "Out of Scope")
#   raw.githubusercontent.com — raw file content, not a repository page
#   *.github.io              — GitHub Pages sites
_ALLOWED_HOSTS = frozenset({"github.com", "www.github.com"})

# ----------------------------------------------------------------------
# Path rules
# ----------------------------------------------------------------------

# Top-level GitHub paths that look like an owner but are not. `github.com/topics/rust`
# has two segments and would otherwise canonicalize to the "repository"
# `topics/rust`.
_RESERVED_OWNERS = frozenset(
    {
        "about",
        "account",
        "apps",
        "codespaces",
        "collections",
        "contact",
        "customer-stories",
        "dashboard",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "join",
        "login",
        "logout",
        "marketplace",
        "new",
        "notifications",
        "organizations",
        "orgs",
        "pricing",
        "pulls",
        "search",
        "security",
        "sessions",
        "settings",
        "signup",
        "sponsors",
        "stars",
        "topics",
        "trending",
        "users",
        "watching",
    }
)

# Owner: alphanumerics and single hyphens, no leading/trailing hyphen, max 39 chars.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# Repository: alphanumerics, hyphen, underscore, dot. Max 100 chars.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# ----------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------

# Matches an https/http URL on a github.com-ish host. Kept broad on the HOST so
# that lookalikes such as `github.com.evil.example` are captured in full and then
# rejected by `canonicalize` — every accept/reject decision has exactly one
# implementation.
#
# The URL body is an ALLOWLIST of the ASCII characters RFC 3986 permits in a
# path/query/fragment. A blacklist ("anything that is not whitespace") is wrong
# here: post text is Chinese and has no spaces, so `.../hello-world，顺便还有`
# would be swallowed whole. Restricting the body to ASCII URL characters makes
# every CJK character — and every full-width punctuation mark — terminate the
# match naturally.
_URL_RE = re.compile(
    r"""(?<![\w@/.-])            # not glued to a word, an email, or a longer URL
        (?P<url>
            https?://
            (?:[A-Za-z0-9-]+\.)*      # optional leading subdomains
            github\.com
            (?:\.[A-Za-z0-9-]+)*      # trailing labels, so lookalike hosts are captured
            (?::[0-9]+)?              # port
            (?:[/?\#][A-Za-z0-9\-._~%!$&'()*+,;=:@/?\#\[\]]*)?
        )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Characters that commonly trail a URL in prose rather than belong to it. Chinese
# punctuation is included because post text is Chinese.
_TRAILING_JUNK = ".,;:!?)]}'\">、。，；：！？）】」》…"


def _strip_trailing_junk(url: str) -> str:
    """Trim prose punctuation from the end of a matched URL.

    `见 https://github.com/a/b。` and `(https://github.com/a/b)` are both common.
    Balanced parentheses are kept when the URL actually opened one, so
    `.../foo_(bar)` survives.
    """
    while url and url[-1] in _TRAILING_JUNK:
        if url[-1] == ")" and url.count("(") > url.count(")"):
            break
        url = url[:-1]
    return url


# ----------------------------------------------------------------------
# Result
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepositoryCandidate:
    """One GitHub repository found in post text.

    `canonical_url` is the global identity (`projects.canonical_url`).
    `evidence_url` is the URL exactly as written in the post, kept as proof that
    the canonicalization was derived and not invented.
    """

    canonical_url: str
    owner: str
    repo: str
    evidence_url: str


# ----------------------------------------------------------------------
# Canonicalization
# ----------------------------------------------------------------------


def canonicalize(url: str) -> RepositoryCandidate | None:
    """Return the repository identity for `url`, or None if it is not a repository.

    Returning None — rather than raising — is deliberate: non-repository GitHub
    links are ordinary content in a forum post, not errors.
    """
    candidate = url.strip()
    if not candidate:
        return None

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    if parts.scheme.lower() not in {"http", "https"}:
        return None

    # `hostname` lowercases and strips any port and userinfo, which is what makes
    # `https://user@GitHub.com:443/a/b` and `https://github.com/a/b` compare equal.
    host = parts.hostname
    if host is None or host.lower() not in _ALLOWED_HOSTS:
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2:
        # A single segment is a profile or organization page, not a repository.
        return None

    owner, repo = segments[0], segments[1]

    if owner.lower() in _RESERVED_OWNERS:
        return None
    if not _OWNER_RE.match(owner):
        return None

    # Trailing `.git` is a clone URL, not part of the name.
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]

    # `.` and `..` are path traversal, never repository names.
    if repo in {".", ".."} or not _REPO_RE.match(repo):
        return None

    owner_key = owner.lower()
    repo_key = repo.lower()

    return RepositoryCandidate(
        canonical_url=f"https://github.com/{owner_key}/{repo_key}",
        owner=owner_key,
        repo=repo_key,
        evidence_url=candidate,
    )


def extract_repository_candidates(text: str) -> list[RepositoryCandidate]:
    """Every distinct repository mentioned in `text`, in order of first appearance.

    Deduplicated by canonical URL: a post that links `owner/repo`, then
    `owner/repo/issues/1`, mentions one project. The first URL seen wins as the
    evidence link, which keeps the most direct reference as the proof.
    """
    if not text:
        return []

    found: dict[str, RepositoryCandidate] = {}

    for match in _URL_RE.finditer(text):
        url = _strip_trailing_junk(match.group("url"))
        candidate = canonicalize(url)
        if candidate is not None and candidate.canonical_url not in found:
            found[candidate.canonical_url] = candidate

    return list(found.values())


def contains_repository_url(text: str) -> bool:
    """Whether `text` mentions at least one repository.

    Used for the PRD's reply-retention rule (a reply is kept only if its original
    content contains an explicit GitHub URL) and for the "skip the LLM entirely"
    decision. Short-circuits instead of building the full candidate list.
    """
    if not text:
        return False

    return any(
        canonicalize(_strip_trailing_junk(match.group("url"))) is not None
        for match in _URL_RE.finditer(text)
    )
