import functools
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from git import Repo, exc
from github import Auth, Github, PullRequest
from jinja2 import Template
from packaging.version import parse

VERSION = r"(?P<wpilib_version>\d+\.\d+\.\d+)"
WPILIB_REGEX = rf'(id "edu\.wpi\.first\.GradleRIO" version )"{VERSION}"'
UPDATED_DEPS = []
BRANCH_NAME = "vendordeps-update"
SCRIPT_PATH = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", None)
BASE_BRANCH = os.getenv("BASE_BRANCH", "main")
REPO_PATH = os.getenv("REPO_PATH", None)
PR_TITLE = []
VENDOR_DEP_MARKETPLACE_URL = (
    "https://frcmaven.wpi.edu/artifactory/vendordeps/vendordep-marketplace"
)


def getProjectYear() -> str:
    settings = Path.cwd().joinpath(".wpilib\\wpilib_preferences.json")
    with settings.open(mode="r", encoding="utf-8") as f:
        wpilib_settings = json.load(f)
        return wpilib_settings.get("projectYear", None)
    return None


def loadFileFromUrl(url: str) -> list | dict:
    response = requests.get(url)
    if response.status_code >= 200 and response.status_code < 400:
        file = response.json()
        return file
    return None


def compareVersions(item1: str, item2: str):
    verstion1 = item1.get("version")
    verstion2 = item2.get("version")
    if parse(verstion1) < parse(verstion2):
        return -1
    elif parse(verstion1) > parse(verstion2):
        return 1
    else:
        return 0


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
    projectYear = getProjectYear()
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
    wpilib_latest_release = wpilib_repo.get_latest_release()
    wpilib_latest_version = wpilib_latest_release.tag_name.replace("v", "")
    if parse(wpilib_latest_version) > parse(
        wpilib_version
    ) and wpilib_latest_version.startswith(projectYear):
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
                "website": f"{wpilib_latest_release.html_url}",
            }
        )
        PR_TITLE.append("WPILib")
    else:
        print("No new version of WPILIB.")
    print("Checking for Vendor Dep Updates")
    manifestURL = f"{VENDOR_DEP_MARKETPLACE_URL}/{projectYear}.json"
    onlineDeps = loadFileFromUrl(manifestURL)
    _dir = Path.cwd().joinpath("vendordeps")
    for file in _dir.glob("*.json"):
        with file.open(mode="r", encoding="utf-8") as f:
            vendor: dict = json.load(f)
        uuid = vendor.get("uuid")
        version = vendor.get("version")
        name = vendor.get("name")
        print(name)
        vendor_versions = [x for x in onlineDeps if x.get("uuid", "") == uuid]
        if len(vendor_versions) == 0:
            continue
        vendor_versions.sort(key=functools.cmp_to_key(compareVersions), reverse=True)
        new_vendor = vendor_versions[0]
        new_version = new_vendor.get("version")
        if parse(new_version) <= parse(version):
            continue
        print(f"{version} -> {new_version}")
        UPDATED_DEPS.append(
            {
                "name": name,
                "old_version": version,
                "new_version": new_version,
                "website": new_vendor.get("website"),
            }
        )
        url = new_vendor.get("path", "")
        if not url.startswith("http"):
            url = f"{VENDOR_DEP_MARKETPLACE_URL}/{url}"
        new_file = os.path.basename(urlparse(url).path)
        new_file_data = loadFileFromUrl(url)
        with _dir.joinpath(new_file).open(mode="w", encoding="utf-8") as f:
            json.dump(new_file_data, f, indent=4)
        if new_file != file.name:
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
        print(body)
    title = f"{" and ".join(PR_TITLE)} Updates"
    if pulls.totalCount == 0:
        gh_repo.create_pull(
            base=BASE_BRANCH, head=BRANCH_NAME, title=title, body=body, draft=True
        )
    elif pulls.totalCount == 1:
        pull: PullRequest = pulls[0]
        pull.edit(body=body, title=title)
