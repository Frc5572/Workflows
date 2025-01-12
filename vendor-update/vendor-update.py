import json
import os
import re
import sys
from pathlib import Path

import requests
from git import Repo, exc
from github import Auth, Github, PullRequest
from jinja2 import Template
from packaging.version import parse

VENDOR_REGEX = r"(?P<name>[^\s-]+)(?P<versioned>-\d+\.\d+\.\d+)?"
VERSION = r"(?P<wpilib_version>\d+\.\d+\.\d+)"
WPILIB_REGEX = rf'(id "edu\.wpi\.first\.GradleRIO" version )"{VERSION}"'
UPDATED_DEPS = []
BRANCH_NAME = "vendordeps-update"
SCRIPT_PATH = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", None)
BASE_BRANCH = os.getenv("BASE_BRANCH", "main")
REPO_PATH = os.getenv("REPO_PATH", None)
PR_TITLE = []


if __name__ == "__main__":
    auth = Auth.Token(GITHUB_TOKEN)
    g = Github(auth=auth)
    repo = Repo(Path.cwd())
    try:
        repo.git.checkout(BRANCH_NAME)
        current_branch = repo.active_branch
        target_branch = repo.heads[BASE_BRANCH]
        rebase_branch = repo.create_head("temp_rebase_branch", target_branch)
        repo.head.reference = rebase_branch
        repo.head.reset(index=True, working_tree=True)
        try:
            repo.git.rebase(current_branch)
        except exc.GitCommandError as e:
            # Handle rebase conflicts if any
            print("Rebase conflicts occurred. Resolve them manually.")
            print(e)
        else:
            # Delete the original branch
            repo.delete_head(current_branch)
            # Rename the rebased branch to the original branch name
            rebase_branch.rename(current_branch)
            # Update the remote branch (if needed)
    except exc.GitCommandError:
        repo.git.checkout("-b", BRANCH_NAME)

    print("Checking for WPILIB Updates")
    update_wpilib = False
    build_gradle = Path.cwd().joinpath("build.gradle")
    with build_gradle.open(mode="r", encoding="utf-8") as f:
        build_file = f.read()
    wpilib_re = re.search(WPILIB_REGEX, build_file, re.MULTILINE)
    wpilib_version = wpilib_re.groupdict().get("wpilib_version", None)
    if wpilib_version is None:
        print("Could not determine current WPILIB version")
        sys.exit(1)
    wpilib_repo = g.get_repo("wpilibsuite/allwpilib")
    wpilib_latest_version = wpilib_repo.get_latest_release().tag_name.replace("v", "")
    if parse(wpilib_latest_version) > parse(wpilib_version):
        print(f"New WPILIB Version: {wpilib_latest_version}. Updating build.gradle.")
        with build_gradle.open(mode="w", encoding="utf-8") as f:
            new_build = re.sub(
                WPILIB_REGEX, rf'\1"{wpilib_latest_version}"', build_file
            )
            f.write(new_build)
        update_wpilib = True
        repo.git.add("build.gradle")
        UPDATED_DEPS.append(
            {
                "name": "WPILib",
                "old_version": wpilib_version,
                "new_version": wpilib_latest_version,
            }
        )
        PR_TITLE.append("WPILib")
    else:
        print("No new version of WPILIB.")
    print("Checking for Vendor Dep Updates")
    _dir = Path.cwd().joinpath("vendordeps")
    for file in _dir.glob("*.json"):
        print(file.stem)
        with file.open(mode="r", encoding="utf-8") as f:
            vendor: dict = json.load(f)
        file_regex = re.match(VENDOR_REGEX, file.stem)
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
    if len(modified_deps) > 0:
        repo.git.add("vendordeps/*")
        PR_TITLE.append("Vendor Dependency")
    else:
        print("No vendor updates")
    if len(modified_deps) == 0 and not update_wpilib:
        sys.exit(0)

    repo.index.commit(f"Updating {', '.join([x.get('name') for x in UPDATED_DEPS])}")
    repo.git.push("--force", "--set-upstream", "origin", repo.head.ref)

    gh_repo = g.get_repo(REPO_PATH)
    pulls = gh_repo.get_pulls(
        state="open", sort="created", base=BASE_BRANCH, head=f"Frc5572:{BRANCH_NAME}"
    )
    with open(SCRIPT_PATH.joinpath("pr-template.j2")) as f:
        body = Template(f.read()).render(deps=UPDATED_DEPS)
    title = f"{" and ".join(PR_TITLE)} Updates"
    if pulls.totalCount == 0:
        gh_repo.create_pull(
            base=BASE_BRANCH, head=BRANCH_NAME, title=title, body=body, draft=True
        )
    elif pulls.totalCount == 1:
        pull: PullRequest = pulls[0]
        pull.edit(body=body, title=title)
