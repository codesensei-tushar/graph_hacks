"""A fake GitHub client for tests and the demo replay, driven by the scripted repos.

Stands in for ``GitHubClient`` at the seam the backfill and the webhook handler reach
it through (``client_for``), so no app JWT, installation token, or network is
involved. Scripted dataclasses in, GitHub-shaped dicts out; nothing else.
"""

from collections.abc import Iterable

from src.seed.quillfern_workspace import GITHUB_USERS, Repo, RepoIssue, RepoPullRequest

UPDATED_AT = "2026-10-01T00:00:00Z"


def github_user(login: str) -> dict:
    return {"id": GITHUB_USERS[login], "login": login}


def repo_payload(repo: Repo) -> dict:
    return {"id": repo.id, "full_name": repo.full_name, "private": repo.private, "html_url": f"https://github.com/{repo.full_name}"}


def pull_request_payload(repo: Repo, pr: RepoPullRequest) -> dict:
    return {
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": "closed" if pr.merged else "open",
        "merged": pr.merged,
        "html_url": f"https://github.com/{repo.full_name}/pull/{pr.number}",
        "user": github_user(pr.author),
        "updated_at": UPDATED_AT,
    }


def issue_payload(repo: Repo, issue: RepoIssue) -> dict:
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": "open",
        "html_url": f"https://github.com/{repo.full_name}/issues/{issue.number}",
        "user": github_user(issue.author),
        "assignees": [github_user(login) for login in issue.assignees],
        "updated_at": UPDATED_AT,
    }


class FakeGitHubClient:
    def __init__(self, repos: Iterable[Repo], installation_id: int):
        self.installation_id = installation_id
        self._repos = {repo.full_name: repo for repo in repos}

    def _pull_request(self, full_name: str, number: int) -> RepoPullRequest:
        return next(pr for pr in self._repos[full_name].pull_requests if pr.number == number)

    def list_repositories(self) -> list[dict]:
        return [repo_payload(repo) for repo in self._repos.values()]

    def list_collaborators(self, full_name: str) -> list[dict]:
        return [github_user(login) for login in self._repos[full_name].collaborators]

    def list_pull_requests(self, full_name: str, limit: int) -> list[dict]:
        repo = self._repos[full_name]
        return [pull_request_payload(repo, pr) for pr in repo.pull_requests][:limit]

    def list_pull_request_files(self, full_name: str, number: int) -> list[dict]:
        return [{"filename": path} for path in self._pull_request(full_name, number).files]

    def list_pull_request_reviews(self, full_name: str, number: int) -> list[dict]:
        return [{"user": github_user(login), "state": "APPROVED"} for login in self._pull_request(full_name, number).reviewers]

    def list_issues(self, full_name: str, limit: int) -> list[dict]:
        repo = self._repos[full_name]
        return [issue_payload(repo, issue) for issue in repo.issues][:limit]

    # The paged calls the stepped backfill makes (src/domain/github/backfill.py).

    def get_repo(self, full_name: str) -> dict:
        return repo_payload(self._repos[full_name])

    def pull_requests_page(self, full_name: str, page: int, per_page: int, sort: str = "created") -> tuple[list[dict], bool]:
        return _page(self.list_pull_requests(full_name, 10_000), page, per_page)

    def issues_page(self, full_name: str, page: int, per_page: int, since: str | None = None) -> tuple[list[dict], bool]:
        return _page(self.list_issues(full_name, 10_000), page, per_page)


def _page(items: list[dict], page: int, per_page: int) -> tuple[list[dict], bool]:
    return items[(page - 1) * per_page : page * per_page], page * per_page < len(items)
