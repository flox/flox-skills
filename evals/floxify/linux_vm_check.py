#!/usr/bin/env python3
"""E3: activate manifests produced on the Mac inside the Linux VM.

For each result in a real_world.py results JSON that has a manifest and a
lock: clone the repo at the same SHA inside VM `floxify-fresh`, drop in the
produced .flox/env/manifest.toml + manifest.lock, and run
`flox activate -c 'echo __ok__'` (the lock already covers every system in
[options] systems, so Linux resolves from it). Prints one line per repo.

Usage: python3 linux_vm_check.py results/<run>.json [--vm floxify-fresh]

Needs a lima VM (`limactl start --name=floxify-fresh --mount-none
template://ubuntu-24.04`, then Flox from get.flox.dev and `flox auth login`).
Each repo's checkout is removed afterwards and the Nix store garbage-collected,
so large closures (GHC, JDKs) don't fill the VM's disk.
"""
import json
import shlex
import subprocess
import sys

VM = sys.argv[sys.argv.index("--vm") + 1] if "--vm" in sys.argv else "floxify-fresh"


def check(r):
    d = f"/tmp/e3/{r['id']}"
    script = "\n".join([
        "set -e",
        f"rm -rf {d} && mkdir -p {d} && cd {d}",
        "git init -q",
        f"git fetch -q --depth 1 {shlex.quote(r['repo_url'])} {r['sha']} && git checkout -q FETCH_HEAD",
        "rm -rf .flox && mkdir -p .flox/env",
        f"""printf '{{"name":"{r['id']}","version":1}}' > .flox/env.json""",
        f"cat > .flox/env/manifest.toml <<'__M__'\n{r['manifest']}\n__M__",
        f"cat > .flox/env/manifest.lock <<'__L__'\n{r['manifest_lock']}\n__L__",
        "set +e",
        "out=$(timeout 1800 flox activate -c 'echo __ok__' 2>&1); rc=$?",
        'echo "$out" | grep -q __ok__ && echo RESULT=ok || { echo RESULT=FAIL rc=$rc; echo "$out" | tail -8; }',
        f"cd / && rm -rf {d}",
        "sudo nix-collect-garbage -d >/dev/null 2>&1 || true",
    ])
    p = subprocess.run(["limactl", "shell", VM, "--", "bash", "-lc", script],
                       capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    return out


def main():
    results = json.load(open(sys.argv[1]))
    runs = results.get("results", results) if isinstance(results, dict) else results
    flat = []
    for r in runs:
        flat += r.get("runs", [r]) if isinstance(r, dict) else []
    for r in flat:
        if not (r.get("manifest") and r.get("manifest_lock")):
            print(f"{r.get('id')}: SKIP (no manifest or lock)")
            continue
        out = check(r)
        status = "ok" if "RESULT=ok" in out else "FAIL"
        print(f"{r['id']}: {status}")
        if status != "ok":
            print("   " + out.replace("\n", "\n   ")[-1500:])


if __name__ == "__main__":
    main()
