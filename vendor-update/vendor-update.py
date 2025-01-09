import json
import re
import sys
from pathlib import Path
import os

import requests
from git import Repo, exc
from github import Auth, Github, PullRequest
from packaging.version import parse
from jinja2 import Template


REGEX = r"(?P<name>[^\s-]+)(?P<versioned>-\d+\.\d+\.\d+)?"
UPDATED_DEPS = []
BRANCH_NAME = "vendordeps-update"
SCRIPT_PATH = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", None)
BASE_BRANCH = os.getenv("BASE_BRANCH", "main")
REPO_PATH = os.getenv("REPO_PATH", None)

if __name__ == "__main__":
    auth = Auth.Token(GITHUB_TOKEN)
    g = Github(auth=auth)
    repo = Repo(Path.cwd())
    try:
        repo.delete_head(BRANCH_NAME)
    except exc.GitCommandError:
        pass
    new_branch = repo.create_head(BRANCH_NAME)
    new_branch.checkout(force=True)

    _dir = Path.cwd().joinpath("vendordeps")
    for file in _dir.glob("*.json"):
        print(file.stem)
        with file.open(mode="r", encoding="utf-8") as f:
            vendor: dict = json.load(f)
        file_regex = re.match(REGEX, file.stem)
        json_url = vendor.get("jsonUrl", None)
        version = vendor.get("version")
        if json_url is None or json_url == "":
            continue
        new_vendor: dict = requests.get(json_url).json()
        new_version = new_vendor.get("version")
        if parse(new_version) <= parse(version):
            continue
        UPDATED_DEPS.append(
            {
                "name": file_regex.groupdict().get("name", None),
                "old_version": version,
                "new_version": new_version,
            }
        )
        file_version = ""
        if file_regex.groupdict().get("versioned", None) is not None:
            file_version = f"-{new_version}"
        new_file = f"{file_regex.groupdict().get("name", None)}{file_version}.json"
        with _dir.joinpath(new_file).open(mode="w", encoding="utf-8") as f:
            new_vendor["fileName"] = new_file
            json.dump(new_vendor, f, indent=4)
        file.unlink()
    untracked = repo.untracked_files
    diffs = [x.a_path for x in repo.index.diff(None)]
    modified_deps = [x for x in untracked + diffs if x.startswith("vendordeps")]
    if len(modified_deps) == 0:
        print("No vendor updates")
        sys.exit(0)
    repo.git.add("vendordeps/*")
    repo.index.commit("Updating Vendor Dependencies again")
    repo.git.push("--force", "--set-upstream", "origin", repo.head.ref)

    gh_repo = g.get_repo(REPO_PATH)
    pulls = gh_repo.get_pulls(
        state="open", sort="created", base=BASE_BRANCH, head=f"Frc5572:{BRANCH_NAME}"
    )
    with open(SCRIPT_PATH.joinpath("pr-template.j2")) as f:
        body = Template(f.read()).render(deps=UPDATED_DEPS)
    if pulls.totalCount == 0:
        gh_repo.create_pull(base=BASE_BRANCH, head=BRANCH_NAME, title="Vendor Dependency Updates", body=body, draft=True)
    elif pulls.totalCount == 1:
        pull: PullRequest = pulls[0]
        pull.edit(body=body)
