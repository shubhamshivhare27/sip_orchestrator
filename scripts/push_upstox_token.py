"""
scripts/push_upstox_token.py
─────────────────────────────
ADD THIS TO YOUR ENGINE 2 REPO. Not this repo.

Add as last step in Engine 2's refresh_upstox_token.yml:

  - name: Push token to orchestrator
    env:
      UPSTOX_TOKEN:          ${{ secrets.UPSTOX_TOKEN }}
      UPSTOX_TOKEN_EXPIRY:   ${{ secrets.UPSTOX_TOKEN_EXPIRY }}
      ORCHESTRATOR_REPO_PAT: ${{ secrets.ORCHESTRATOR_REPO_PAT }}
      ORCHESTRATOR_OWNER:    YOUR_GITHUB_USERNAME
      ORCHESTRATOR_REPO:     sip_orchestrator
    run: python scripts/push_upstox_token.py

Add to Engine 2 requirements.txt: pynacl>=1.5.0
"""
import os, sys, base64, requests

def push(owner, repo, name, value, pat):
    h = {"Authorization":f"Bearer {pat}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
    r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",headers=h,timeout=15)
    r.raise_for_status()
    kd = r.json()
    from nacl import encoding, public as np
    pub = base64.b64decode(kd["key"])
    enc = base64.b64encode(np.SealedBox(np.PublicKey(pub)).encrypt(value.encode())).decode()
    r2 = requests.put(f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{name}",
                      headers=h,json={"encrypted_value":enc,"key_id":kd["key_id"]},timeout=15)
    r2.raise_for_status()
    print(f"Pushed {name} to {owner}/{repo} (status {r2.status_code})")

token  = os.environ.get("UPSTOX_TOKEN","")
expiry = os.environ.get("UPSTOX_TOKEN_EXPIRY","")
pat    = os.environ.get("ORCHESTRATOR_REPO_PAT","")
owner  = os.environ.get("ORCHESTRATOR_OWNER","")
repo   = os.environ.get("ORCHESTRATOR_REPO","sip_orchestrator")

if not all([token,pat,owner]):
    print("Missing: UPSTOX_TOKEN, ORCHESTRATOR_REPO_PAT, ORCHESTRATOR_OWNER"); sys.exit(1)

push(owner, repo, "UPSTOX_TOKEN", token, pat)
if expiry:
    push(owner, repo, "UPSTOX_TOKEN_EXPIRY", expiry, pat)
