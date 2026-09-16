# claudeusage

Measure whether your Claude subscription is actually paying off — from local logs, with no upload and no Git integration.

*[한국어 문서: README.ko.md](README.ko.md) — the Korean version is the working notebook, with the full measurements and the mistakes we made getting there.*

## Why this exists

On a subscription you don't pay per token, so nothing tells you whether you are using it well. Existing personal tools count **how much** you used. That is the wrong number: burning tokens in circles scores high. Meta ran an internal leaderboard like that and shut it down — people started idling AI to climb it.

Tools that measure quality do exist, but they are all built for teams: they need your Git repo, your PRs, your issue tracker. That leaves a gap.

|  | Team tools | claudeusage |
| --- | --- | --- |
| Who is measured | A lead measures the team | You measure yourself |
| Needs | Git repo, PRs, tracker | One local log directory |
| Measures | Return on team investment | Whether your subscription pays off |
| Price | SaaS or self-hosted | Free, runs locally |

Git-based tools track code with `git blame`, so anything you never committed is invisible to them. This reconstructs the edit history itself, so experiments and code you rewrote before committing still count.

## What it measures

**Lines that survived.** Every edit is folded in time order, so only what was still there at the end of the session counts. Code you rewrote three turns later drops out by itself. You cannot game the score by idling.

**Waste, in money.** Rework, failures and rejections add up to 4.1% here. Money spent because conversations got long: **12.3%**. So the useful advice is not "make fewer mistakes" — it's **"start fresh conversations more often."**

**What the rate limit actually eats.** Your real constraint is the limit gauge, not dollars. Cache reads are most of the cost but weigh about 1/70 of fresh input against the limit. **Saving money and saving limit are different skills.**

**What chat ate.** Limits apply to the whole account, so claude.ai chat and the mobile app drain the same gauge — while leaving no local trace. Any log-only tool undercounts. This one subtracts what the logs explain from what the gauge actually did, and checks that estimate against the per-product breakdown Anthropic returns.

## Install

Requires Python 3 (standard library only, no dependencies), the `claude` CLI, and macOS for the live-limit tool.

```bash
git clone <this repo> && cd claudeusage
./install.sh          # installs the skill + registers the MCP server (user scope)
claude mcp list       # claudeusage … ✔ Connected
```

`./install.sh --uninstall` reverses it. The installer writes the repo's real path into the installed skill, so the checkout can live anywhere.

## Use it from Claude

Ask in plain language — "am I getting my money's worth?", "why is my limit draining so fast?", "how much of my limit did chat eat?" The skill picks the right tool.

| MCP tool | What it answers |
| --- | --- |
| `subscription_value` | Lines that survived, waste, cost by activity |
| `limit_breakdown` | What drives the 5-hour and weekly limits |
| `chat_share` | How much of the limit chat ate |
| `current_limits` | Limits right now, plus the per-product breakdown |

## Use it from the shell

```bash
python3 tools/cc-value.py --all            # all projects
python3 tools/cc-value.py --project PATH --waste
python3 tools/cc-limit.py                  # limit burn per 5-hour window
python3 tools/cc-limit.py --fit            # fit: what the gauge weighs
python3 tools/cc-chat.py                   # chat's share, weekly
python3 tools/cc-usage.py                  # live limits + product breakdown
```

## How the chat estimate works

Three sources, each blind in a different way, so they are merged:

| Source | Gives | Blind when |
| --- | --- | --- |
| Status line samples | Gauge readings with reset times | Claude Code isn't running |
| Claude desktop app history | Gauge readings every 15 min | The app isn't running |
| OAuth usage endpoint | **Per-product truth** (Claude Code / chat / Cowork) | Only the current weekly window |

The estimate is a subtraction: `chat = gauge rise − what local logs explain`. The "explained" part uses per-model weights fitted by `cc-limit.py`, fitted only on windows with no suspected off-log usage — otherwise chat usage inflates the weights and the estimator goes blind to itself.

Two things had to be corrected, both found by running it on real data:

1. **Half-observed windows tilt the subtraction.** If the gauge is only watched for part of a window, the rise reads low while the attributed cost reads high. Only windows with a known reset time, watched from zero, are counted (55 of 107 here).
2. **Log-derived cost runs 6–9% low** — background calls never reach the logs. Left alone, that gap becomes fake chat usage. Each window is scaled against the status line's own total.

`python3 tools/check-chat.py` verifies 15 invariants, including an anchor: a real window where chat was confirmed by hand.

## Accuracy, honestly

- The per-product breakdown is ground truth, but only one weekly window of it has been collected so far. Calling the estimator "validated" needs more.
- Model weights come from 5–7 clean windows for the less-used models. Direction is solid; the exact multiplier still moves.
- Limit gauges are integers. One change point is coarse; answers are averages over ~1,200 of them.
- `cc-usage.py` reads Claude Code's credentials from the macOS keychain, sends them only to `api.anthropic.com`, and never prints or stores them. What it records is an allowlist: timestamp, limit kind, model name, percentage, reset time, product shares.

## What's not here yet

Other vendors. Merging ChatGPT and Gemini exports was the original plan for "combine coding and chat"; this covers the Claude account only. That part is about subscription money rather than rate limits, so it needs its own metrics.

## License

MIT
