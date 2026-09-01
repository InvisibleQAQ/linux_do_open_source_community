"""Table-driven tests for the GitHub URL canonicalizer.

The PRD asks for exhaustive coverage of repository roots, subpaths, case
normalization, invalid profile/organization URLs and deceptive hosts. This module
is the anti-hallucination foundation — the LLM may only pick from what it
extracts — so a false accept here becomes bad published data.
"""

from __future__ import annotations

import pytest

from linuxdo_oss.domain.github_url import (
    canonicalize,
    contains_repository_url,
    extract_repository_candidates,
)

# ----------------------------------------------------------------------
# Accepted: repository roots, subpaths, and the noise around them
# ----------------------------------------------------------------------

ACCEPTED = [
    # (url, expected owner, expected repo)
    ("https://github.com/octocat/hello-world", "octocat", "hello-world"),
    ("http://github.com/octocat/hello-world", "octocat", "hello-world"),
    ("https://www.github.com/octocat/hello-world", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/", "octocat", "hello-world"),
    # Subpaths the PRD names explicitly
    ("https://github.com/octocat/hello-world/issues", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/issues/42", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/pull/7", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/tree/main", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/tree/main/src/deep/path", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/blob/main/README.md", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/releases", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/releases/tag/v1.0.0", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/wiki", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/actions", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/stargazers", "octocat", "hello-world"),
    # Query and fragment are not part of the identity
    ("https://github.com/octocat/hello-world?tab=readme-ov-file", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world#installation", "octocat", "hello-world"),
    ("https://github.com/octocat/hello-world/issues?q=is%3Aopen", "octocat", "hello-world"),
    # Clone URL
    ("https://github.com/octocat/hello-world.git", "octocat", "hello-world"),
    # Port and userinfo are stripped by `hostname`
    ("https://github.com:443/octocat/hello-world", "octocat", "hello-world"),
    # Repository names may contain dots, underscores and hyphens
    ("https://github.com/tj/n", "tj", "n"),
    ("https://github.com/some-org/some.repo_name-2", "some-org", "some.repo_name-2"),
    ("https://github.com/a/b.js", "a", "b.js"),
    ("https://github.com/user/dotfiles.git.backup", "user", "dotfiles.git.backup"),
]


@pytest.mark.parametrize(("url", "owner", "repo"), ACCEPTED)
def test_accepted_urls_canonicalize(url: str, owner: str, repo: str) -> None:
    candidate = canonicalize(url)

    assert candidate is not None, f"should have been accepted: {url}"
    assert candidate.owner == owner
    assert candidate.repo == repo
    assert candidate.canonical_url == f"https://github.com/{owner}/{repo}"
    # The original URL survives verbatim as evidence.
    assert candidate.evidence_url == url


# ----------------------------------------------------------------------
# Case normalization
# ----------------------------------------------------------------------

CASE_VARIANTS = [
    "https://github.com/OctoCat/Hello-World",
    "https://GitHub.com/octocat/hello-world",
    "HTTPS://GITHUB.COM/OCTOCAT/HELLO-WORLD",
    "https://github.com/octocat/HELLO-WORLD/issues/1",
    "https://WWW.GitHub.COM/OctoCat/Hello-World.GIT",
]


@pytest.mark.parametrize("url", CASE_VARIANTS)
def test_case_variants_collapse_onto_one_identity(url: str) -> None:
    """GitHub owner/repo are case-insensitive for lookup, so these are all the same
    repository and must produce one `projects` row."""
    candidate = canonicalize(url)

    assert candidate is not None
    assert candidate.canonical_url == "https://github.com/octocat/hello-world"


# ----------------------------------------------------------------------
# Rejected: not repositories
# ----------------------------------------------------------------------

REJECTED = [
    # Profile / organization pages — one path segment
    "https://github.com/octocat",
    "https://github.com/octocat/",
    "https://github.com",
    "https://github.com/",
    # Reserved top-level paths that syntactically look like owner/repo
    "https://github.com/topics/rust",
    "https://github.com/search?q=rss",
    "https://github.com/marketplace/actions/checkout",
    "https://github.com/orgs/github/repositories",
    "https://github.com/settings/profile",
    "https://github.com/explore/trending",
    "https://github.com/sponsors/octocat",
    "https://github.com/notifications/subscriptions",
    "https://github.com/users/octocat",
    "https://github.com/new/import",
    "https://github.com/pricing/plans",
    "https://github.com/features/actions",
    "https://github.com/collections/clean-code-linters",
    "https://github.com/apps/dependabot",
    "https://github.com/login/oauth",
    "https://github.com/pulls/review-requested",
    "https://github.com/issues/assigned",
    # Gists are out of scope
    "https://gist.github.com/octocat/6cad326836d38bd3a7ae",
    # Raw content and Pages are not repository pages
    "https://raw.githubusercontent.com/octocat/hello-world/main/README.md",
    "https://octocat.github.io/hello-world",
    "https://octocat.github.io",
    # Path traversal
    "https://github.com/octocat/..",
    "https://github.com/octocat/.",
    # Invalid owner shapes
    "https://github.com/-octocat/repo",
    "https://github.com/octocat-/repo",
    "https://github.com/octo--cat/repo",
    "https://github.com/" + "a" * 40 + "/repo",
    # Invalid repo shapes
    "https://github.com/octocat/" + "r" * 101,
    "https://github.com/octocat/re po",
    # Non-http schemes
    "git@github.com:octocat/hello-world.git",
    "ssh://git@github.com/octocat/hello-world.git",
    "ftp://github.com/octocat/hello-world",
    "javascript:alert(1)",
    # Empty / junk
    "",
    "   ",
    "not a url",
    "https://",
]


@pytest.mark.parametrize("url", REJECTED)
def test_rejected_urls_return_none(url: str) -> None:
    assert canonicalize(url) is None, f"should have been rejected: {url}"


# ----------------------------------------------------------------------
# Deceptive hosts — the ones a substring check would let through
# ----------------------------------------------------------------------

DECEPTIVE_HOSTS = [
    "https://github.com.evil.example/octocat/hello-world",
    "https://notgithub.com/octocat/hello-world",
    "https://mygithub.com/octocat/hello-world",
    "https://github.co/octocat/hello-world",
    "https://github.com.co/octocat/hello-world",
    "https://evil.example/github.com/octocat/hello-world",
    "https://evil.example/?redirect=https://github.com/octocat/hello-world",
    "https://github-com.evil.example/octocat/hello-world",
    "https://xn--github-fsa.com/octocat/hello-world",
    "https://evil.example@github.com.attacker.example/a/b",
]


@pytest.mark.parametrize("url", DECEPTIVE_HOSTS)
def test_deceptive_hosts_are_rejected(url: str) -> None:
    """The host check is an exact allowlist for exactly these cases. A substring
    test on 'github.com' accepts every URL in this list."""
    assert canonicalize(url) is None, f"deceptive host should have been rejected: {url}"


def test_userinfo_cannot_smuggle_a_foreign_host() -> None:
    """`https://github.com@evil.example/a/b` has host `evil.example`, not github.com.
    Anyone parsing by string search gets this backwards."""
    assert canonicalize("https://github.com@evil.example/octocat/hello-world") is None


# ----------------------------------------------------------------------
# Extraction from post text
# ----------------------------------------------------------------------


def test_extraction_finds_urls_inside_chinese_prose() -> None:
    text = (
        "推荐这个项目：https://github.com/octocat/hello-world，"
        "顺便还有 https://github.com/acme/tool。另外（https://github.com/foo/bar）也不错。"
    )

    candidates = extract_repository_candidates(text)

    assert [c.canonical_url for c in candidates] == [
        "https://github.com/octocat/hello-world",
        "https://github.com/acme/tool",
        "https://github.com/foo/bar",
    ]
    # Chinese and ASCII punctuation must not end up inside the evidence URL.
    for candidate in candidates:
        assert not candidate.evidence_url.endswith(("，", "。", ")", "）"))


def test_extraction_preserves_order_and_dedupes_by_identity() -> None:
    """A post that links the root, then an issue, then a tree of the same repo is
    mentioning one project. The first, most direct reference stays as evidence."""
    text = (
        "https://github.com/octocat/hello-world "
        "https://github.com/octocat/hello-world/issues/1 "
        "https://github.com/OCTOCAT/Hello-World/tree/main "
        "https://github.com/acme/tool"
    )

    candidates = extract_repository_candidates(text)

    assert len(candidates) == 2
    assert candidates[0].canonical_url == "https://github.com/octocat/hello-world"
    assert candidates[0].evidence_url == "https://github.com/octocat/hello-world"
    assert candidates[1].canonical_url == "https://github.com/acme/tool"


@pytest.mark.parametrize(
    "text",
    [
        "见 (https://github.com/octocat/hello-world) 这个",
        "见 （https://github.com/octocat/hello-world） 这个",
        "见 [https://github.com/octocat/hello-world] 这个",
        "见 「https://github.com/octocat/hello-world」 这个",
        "见 https://github.com/octocat/hello-world.",
        "见 https://github.com/octocat/hello-world。",
        "见 https://github.com/octocat/hello-world，然后",
        "见 https://github.com/octocat/hello-world; 然后",
    ],
)
def test_surrounding_punctuation_is_not_part_of_the_url(text: str) -> None:
    """A repository name cannot contain parentheses, brackets or punctuation, so any
    of these leaking into the path would turn a real repository into a rejected
    candidate — a silent data loss, not a visible error."""
    candidates = extract_repository_candidates(text)

    assert len(candidates) == 1, text
    assert candidates[0].canonical_url == "https://github.com/octocat/hello-world"


def test_extraction_ignores_non_repository_github_links() -> None:
    text = (
        "我在 https://github.com/topics/rss 里找的，"
        "作者主页是 https://github.com/octocat，"
        "gist 见 https://gist.github.com/octocat/abc123。"
    )

    assert extract_repository_candidates(text) == []


def test_extraction_ignores_lookalike_hosts_in_prose() -> None:
    text = "小心这个 https://github.com.evil.example/octocat/hello-world 链接"

    assert extract_repository_candidates(text) == []


def test_extraction_does_not_match_inside_an_email_address() -> None:
    assert extract_repository_candidates("联系 admin@github.com/octocat/repo") == []


def test_extraction_on_empty_text() -> None:
    assert extract_repository_candidates("") == []
    assert extract_repository_candidates("   \n  ") == []


# ----------------------------------------------------------------------
# The reply-retention and skip-the-LLM predicate
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("看看 https://github.com/a/b", True),
        ("看看 https://github.com/a/b/issues/1", True),
        ("没有链接的回复", False),
        ("只有主页 https://github.com/octocat", False),
        ("只有 topic 页 https://github.com/topics/rust", False),
        ("假域名 https://notgithub.com/a/b", False),
        ("", False),
    ],
)
def test_contains_repository_url(text: str, expected: bool) -> None:
    """Drives two PRD rules: a reply is retained only when it contains an explicit
    repository URL, and a topic with no candidates settles as `not_relevant`
    without ever calling the LLM."""
    assert contains_repository_url(text) is expected


def test_contains_repository_url_agrees_with_extraction() -> None:
    """The cheap predicate must never disagree with the full extraction, or a topic
    could be sent to the LLM with an empty candidate list — or skipped despite
    having one."""
    samples = [
        "https://github.com/a/b",
        "https://github.com/octocat",
        "https://github.com/topics/x",
        "no links here",
        "https://github.com.evil.example/a/b",
        "多个 https://github.com/a/b 和 https://github.com/c/d",
    ]

    for text in samples:
        assert contains_repository_url(text) is bool(extract_repository_candidates(text)), text
