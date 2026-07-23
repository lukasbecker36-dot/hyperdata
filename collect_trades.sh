#!/usr/bin/env bash
# Collect every arm's trade log into ./live/ with a unique, analysis-friendly name.
# Run on the server, then push (or scp) — see the printed hand-off options.
cd "$(dirname "$0")" || exit 1
mkdir -p live
n=0
for d in paper_5m paper_15m paper_15m_mid paper_15m_boll paper_15m_boll_mid paper_15m_ats; do
  f=$(ls "$d"/trades_*.csv 2>/dev/null | head -1)
  if [ -n "$f" ]; then
    cp "$f" "live/$d.csv"
    echo "  $d -> live/$d.csv ($(($(wc -l < "$f") - 1)) trades)"
    n=$((n+1))
  fi
done
echo "collected $n arms into $(pwd)/live/"
echo
echo "hand off, pick one:"
echo "  (a) push to the repo (I pull & analyze):"
echo "        git add -f live/*.csv && git commit -m 'trade snapshot' && git push origin main"
echo "  (b) copy to your laptop:"
echo "        scp $(pwd)/live/*.csv you@laptop:~/"
