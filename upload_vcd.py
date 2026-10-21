#!/usr/bin/env python3
"""ILA VCD 업로드 도우미

VCD 파일이 모인 폴더(= GitHub 저장소를 clone한 폴더 안)에서 실행하면
  1) git에 아직 올라가지 않은 *.vcd 를 찾아서
  2) add -> commit -> push 하고
  3) raw 주소와 Surfer 링크를 터미널에 출력합니다.

표준 라이브러리만 사용 (Python 3.6+), 외부 패키지 설치 불필요.

사용 예
  python3 upload_vcd.py                # 현재 폴더 기준, 올리기 전에 확인 질문
  python3 upload_vcd.py -y             # 확인 없이 바로 업로드
  python3 upload_vcd.py --dry-run      # 무엇을 올릴지만 보여주고 종료
  python3 upload_vcd.py --all          # 저장소의 모든 VCD 링크 다시 출력 (업로드 없음)
  python3 upload_vcd.py --branch       # 주소에 커밋 해시 대신 브랜치 이름 사용
"""
import argparse
import io
import os
import re
import subprocess
import sys
from urllib.parse import quote

SURFER_URL = "https://app.surfer-project.org/"
RAW_HOST = "https://raw.githubusercontent.com"
MAX_BYTES = 95 * 1024 * 1024          # GitHub는 파일 하나당 100MB 초과 push를 거부함

# 서버 로케일이 UTF-8이 아니어도 한글 메시지가 깨지지 않게
if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def die(msg, code=1):
    print("\n[오류] " + msg)
    sys.exit(code)


def git(args, cwd, check=True):
    """git 실행. (returncode, stdout, stderr) 반환. 파일명 한글 처리를 위해 bytes로 받아 직접 디코딩."""
    p = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = p.stdout.decode("utf-8", errors="replace")
    err = p.stderr.decode("utf-8", errors="replace")
    if check and p.returncode != 0:
        die("git {} 실패\n{}".format(" ".join(args), err.strip()))
    return p.returncode, out, err


def parse_github(url):
    """https/ssh 어느 형태의 GitHub 주소든 (user, repo) 로 분해"""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return (m.group(1), m.group(2)) if m else None


def split_z(out):
    return [x for x in out.split("\0") if x]


def is_vcd(path):
    return path.lower().endswith(".vcd")


def raw_url(user, repo, ref, path):
    return "{}/{}/{}/{}/{}".format(RAW_HOST, user, repo, ref, quote(path, safe="/"))


def main():
    ap = argparse.ArgumentParser(description="git에 안 올라간 VCD를 업로드하고 raw/Surfer 주소를 출력")
    ap.add_argument("dir", nargs="?", default=".", help="VCD가 있는 폴더 (기본: 현재 폴더)")
    ap.add_argument("-y", "--yes", action="store_true", help="확인 질문 없이 바로 업로드")
    ap.add_argument("--dry-run", action="store_true", help="올릴 파일만 보여주고 종료")
    ap.add_argument("--all", action="store_true", help="업로드 없이, 저장소의 모든 VCD 링크 출력")
    ap.add_argument("--branch", action="store_true", help="주소에 커밋 해시 대신 브랜치 이름 사용 (파일을 덮어쓰면 링크 내용도 바뀜)")
    ap.add_argument("--repo-url", help="GitHub 저장소 주소를 직접 지정 (기본: origin 에서 읽음)")
    ap.add_argument("-m", "--message", help="커밋 메시지")
    args = ap.parse_args()

    scan_dir = os.path.abspath(args.dir)
    if not os.path.isdir(scan_dir):
        die("폴더가 없습니다: " + scan_dir)

    # --- 저장소/원격 확인 ---------------------------------------------------
    rc, root, _ = git(["rev-parse", "--show-toplevel"], scan_dir, check=False)
    if rc != 0:
        die("이 폴더는 git 저장소가 아닙니다.\n"
            "       GitHub 저장소를 clone한 폴더 안에서 실행하거나, 폴더 경로를 인자로 주세요.")
    root = root.strip()

    remote = args.repo_url
    if not remote:
        rc, out, _ = git(["remote", "get-url", "origin"], root, check=False)
        if rc != 0:
            die("origin 원격 저장소가 설정되어 있지 않습니다. (git remote -v 로 확인)")
        remote = out.strip()
    parsed = parse_github(remote)
    if not parsed:
        die("GitHub 주소가 아닙니다: {}\n       (raw 주소는 github.com 저장소만 지원합니다)".format(remote))
    user, repo = parsed

    _, branch, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = branch.strip()
    if branch == "HEAD":
        die("detached HEAD 상태입니다. 브랜치로 이동한 뒤 다시 실행하세요. (예: git checkout main)")

    # --- 모든 링크만 출력 -----------------------------------------------------
    if args.all:
        _, out, _ = git(["ls-files", "--full-name", "-z"], scan_dir)
        files = sorted(f for f in split_z(out) if is_vcd(f))
        if not files:
            die("저장소에 VCD가 없습니다.")
        print_links(root, user, repo, branch, files, args.branch)
        return

    # --- 올릴 파일 찾기 -------------------------------------------------------
    _, out, _ = git(["ls-files", "--others", "--exclude-standard", "--full-name", "-z"], scan_dir)
    new_files = sorted(f for f in split_z(out) if is_vcd(f))

    # 이전에 commit만 되고 push 못 한 VCD (push 실패 후 재실행 대비)
    rc, out, _ = git(["diff", "--name-only", "-z", "--diff-filter=AM", "@{u}..HEAD"], root, check=False)
    unpushed_files = sorted(f for f in split_z(out) if is_vcd(f)) if rc == 0 else []
    rc, out, _ = git(["rev-list", "--count", "@{u}..HEAD"], root, check=False)
    has_upstream = rc == 0
    ahead = int(out.strip()) if has_upstream and out.strip().isdigit() else 0

    # 너무 큰 파일은 제외
    too_big = [f for f in new_files if os.path.getsize(os.path.join(root, f)) > MAX_BYTES]
    new_files = [f for f in new_files if f not in too_big]
    for f in too_big:
        print("[건너뜀] 100MB 제한에 가까워 업로드하지 않습니다: " + f)

    if not new_files and ahead == 0:
        print("새로 올릴 VCD가 없습니다. (이미 모두 git에 올라가 있음)")
        print("기존 파일의 링크를 다시 보려면:  python3 {} --all".format(os.path.basename(sys.argv[0])))
        return

    if new_files:
        print("git에 아직 없는 VCD {}개:".format(len(new_files)))
        for f in new_files:
            size = os.path.getsize(os.path.join(root, f))
            print("  - {}  ({:.1f} KB)".format(f, size / 1024.0))
            if re.search(r"[^A-Za-z0-9._/\-]", f):
                print("    ※ 파일명에 공백/특수문자가 있습니다. 링크가 깨질 수 있으니 영문·숫자·_-. 로 바꾸길 권장합니다.")
    if ahead:
        print("push 되지 않은 커밋 {}개가 있어 함께 올립니다.".format(ahead))

    if args.dry_run:
        print("\n(--dry-run) 실제 업로드는 하지 않았습니다.")
        return

    if not args.yes:
        ans = input("\n{} 저장소의 '{}' 브랜치로 업로드할까요? [y/N] ".format("{}/{}".format(user, repo), branch)).strip().lower()
        if ans not in ("y", "yes"):
            print("취소했습니다.")
            return

    # --- add / commit ---------------------------------------------------------
    if new_files:
        git(["add", "--"] + new_files, root)
        msg = args.message or "Add ILA VCD: " + ", ".join(os.path.basename(f) for f in new_files)
        if len(msg) > 120:
            msg = "Add {} ILA VCD files".format(len(new_files))
        rc, out, err = git(["commit", "-m", msg, "--"] + new_files, root, check=False)
        if rc != 0:
            hint = ""
            if "user.name" in err or "user.email" in err or "tell me who you are" in err.lower():
                hint = ('\n       먼저 한 번만 설정하세요:\n'
                        '         git config --global user.name  "이름"\n'
                        '         git config --global user.email "메일주소"')
            die("commit 실패\n{}{}".format(err.strip(), hint))

    # --- push (인증 입력이 필요할 수 있으므로 터미널을 그대로 연결) ----------------
    push_cmd = ["git", "push"] if has_upstream else ["git", "push", "-u", "origin", branch]
    print("\npush 중 ...")
    rc = subprocess.call(push_cmd, cwd=root)
    if rc != 0:
        die("push 실패. 위의 git 메시지를 확인하세요.\n"
            "       - 인증 오류: SSH 키 또는 토큰(PAT) 설정 필요\n"
            "       - 'rejected': 원격에 새 커밋이 있음 -> git pull --rebase 후 재실행\n"
            "       - 접속 불가: 서버에서 github.com 접속/프록시 확인\n"
            "       커밋은 로컬에 남아 있으니, 해결 후 이 스크립트를 다시 실행하면 push부터 이어집니다.")

    # --- 결과 출력 ------------------------------------------------------------
    targets = sorted(set(new_files) | set(unpushed_files))
    print_links(root, user, repo, branch, targets, args.branch, header="업로드 완료")


def print_links(root, user, repo, branch, files, use_branch, header="링크"):
    print("\n" + "=" * 72)
    print("{}: {}개   ({}/{} @ {})".format(header, len(files), user, repo, "브랜치 " + branch if use_branch else "커밋 고정"))
    print("=" * 72)
    for f in files:
        if use_branch:
            ref = branch
        else:
            _, h, _ = git(["log", "-n", "1", "--format=%H", "--", f], root)
            ref = h.strip() or branch
        raw = raw_url(user, repo, ref, f)
        print("\n[{}]".format(f))
        print("raw:")
        print(raw)
        print("Surfer 링크 (Confluence에는 이걸 붙여넣기):")
        print(SURFER_URL + "?load_url=" + raw)
    print("\n" + "=" * 72)
    if use_branch:
        print("※ 브랜치 주소는 같은 이름 파일을 덮어쓰면 링크 내용도 함께 바뀝니다.")
    else:
        print("※ 커밋 고정 주소라 나중에 파일을 수정해도 이 링크의 내용은 그대로 유지됩니다.")


if __name__ == "__main__":
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # | head, | less 로 끊어도 오류 없이 종료
    except (ImportError, AttributeError, ValueError):
        pass
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        sys.exit(130)