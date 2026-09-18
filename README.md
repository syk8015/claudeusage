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

Requires Python 3.9+ (standard library only, no dependencies) and macOS for the live-limit tool.

```bash
git clone https://github.com/syk8015/claudeusage && cd claudeusage
./install.sh                   # package + skill + MCP server (user scope)
claude mcp list                # claudeusage … ✔ Connected
```

`install.sh` installs the package (trying `uv`, `pipx`, `pip`, then a dedicated venv — modern Pythons block system installs), drops the Claude Code skill in `~/.claude/skills/`, and registers the MCP server. `./install.sh --uninstall` reverses it.

That gives you the `claudeusage` command and the `claudeusage-mcp` server. A PyPI release (`pip install claudeusage`) is built and tested but not published yet.

### One more step for the limit tools

Claude Code shows your limit gauge and throws it away — nothing stores it. `claudeusage limit` and `claudeusage chat` need that history, so a collector has to sit on your status line. Add this to `~/.claude/settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "claudeusage statusline --print"
}
```

Already have a status line you like? Drop `--print` and pipe into it — the collector passes the JSON straight through:

```json
"command": "claudeusage statusline | bash ~/.claude/my-statusline.sh"
```

Nothing edits your settings for you — your status line is yours.

**`claudeusage value` and `claudeusage usage` work right away**, with no collector. The limit analysis gets useful after a few days of samples.

Samples live in `~/.claudeusage/` (override with `CLAUDEUSAGE_DATA`). Running from a clone keeps using the repo's own `data/` directory, so an existing history is never orphaned.

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
claudeusage value --all              # all projects
claudeusage value --project PATH --waste
claudeusage limit                    # limit burn per 5-hour window
claudeusage limit --fit              # fit: what the gauge weighs
claudeusage chat                     # chat's share, weekly
claudeusage usage                    # live limits + product breakdown
claudeusage check                    # both consistency checkers
```

Output is English by default. `CLAUDEUSAGE_LANG=ko` switches it to Korean.

## How the chat estimate works

Three sources, each blind in a different way, so they are merged:

| Source | Gives | Blind when |
| --- | --- | --- |
| Status line samples (`claudeusage statusline`) | Gauge readings with reset times | Claude Code isn't running |
| Claude desktop app history | Gauge readings every 15 min | The app isn't running |
| OAuth usage endpoint | **Per-product truth** (Claude Code / chat / Cowork) | Only the current weekly window |

The estimate is a subtraction: `chat = gauge rise − what local logs explain`. The "explained" part uses per-model weights fitted by `claudeusage limit`, fitted only on windows with no suspected off-log usage — otherwise chat usage inflates the weights and the estimator goes blind to itself.

Two things had to be corrected, both found by running it on real data:

1. **Half-observed windows tilt the subtraction.** If the gauge is only watched for part of a window, the rise reads low while the attributed cost reads high. Only windows with a known reset time, watched from zero, are counted (55 of 107 here).
2. **Log-derived cost runs 6–9% low** — background calls never reach the logs. Left alone, that gap becomes fake chat usage. Each window is scaled against the status line's own total.

`claudeusage check` verifies 15 invariants, including an anchor: a real window where chat was confirmed by hand.

## Accuracy, honestly

- The per-product breakdown is ground truth, but only one weekly window of it has been collected so far. Calling the estimator "validated" needs more.
- Model weights come from 5–7 clean windows for the less-used models. Direction is solid; the exact multiplier still moves.
- Limit gauges are integers. One change point is coarse; answers are averages over ~1,200 of them.
- `claudeusage usage` reads Claude Code's credentials from the macOS keychain, sends them only to `api.anthropic.com`, and never prints or stores them. What it records is an allowlist: timestamp, limit kind, model name, percentage, reset time, product shares.

## What's not here yet

Other vendors. Merging ChatGPT and Gemini exports was the original plan for "combine coding and chat"; this covers the Claude account only. That part is about subscription money rather than rate limits, so it needs its own metrics.

## Registry

Listed on the official MCP registry as **`io.github.syk8015/claudeusage`**.

<!-- mcp-name: io.github.syk8015/claudeusage -->

Also listed on [Glama](https://glama.ai/mcp/servers/syk8015/claudeusage).

## License

MIT
