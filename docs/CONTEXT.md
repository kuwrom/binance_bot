# Where the desk's knowledge lives

Every piece of knowledge has one home. Other places point to it (by learning id, file or tool name); they don't copy
it. A copy drifts: the rule changes in the journal and the copy keeps saying the old thing.

## The layers
| Layer | What it holds | Limit | How it reaches the session |
|---|---|---|---|
| 1. Always loaded | `CLAUDE.md` (which desk, what you may do, human-only, rules you'd otherwise miss) and `docs/DOCTRINE.md` (how we trade: settled rules only, each citing its learning id) | under 200 lines together | loaded by Claude Code in every session |
| 2. Session start | The brief: desk state, open trades, resting entries, the last session's handoff, the rulebook (settled learnings DOCTRINE doesn't state), what is relevant now, what to re-check, open alerts | 9,000 characters (`cli_brief.BRIEF_MAX_CHARS`) | the SessionStart hook runs `shortbot brief`, also after a compaction |
| 3. At the decision | The setup's checklist (its skill), and inside tool results: `read_coin` (matching learnings, the registry's evidence cards, `past_trades` with their exit lessons), `coin_memory` (the coin's read), `add_learning` (`similar`) | a skill under 5,000 tokens, hard rules first | the skill loads when its setup appears; the tool result carries the rest |
| 4. Looked up | `learnings` search, `journal`, `research_report`, `shortbot.lab`, `docs/`, a skill's reference files (`order-states.md`) | none | the session asks for it |
| 5. Archive | Decisions, acknowledged alerts, closed trades, scans, rejected and superseded learnings, the trial register, git history, `docs/failures/` | none | never shown unasked; research reads it |

Why these limits: Claude Code shows only the first 2,000 characters of a hook output above 10,000 characters, follows
an instruction file less well above 200 lines, and after a compaction keeps only the first 5,000 tokens of a skill
(code.claude.com/docs: hooks, memory, skills). Long context also lowers accuracy on its own ("context rot").

## Where does a new piece of knowledge go?
| You have | Put it in | Not in |
|---|---|---|
| A claim about the market or a trading rule | a learning (`add_learning`: evidence, kind, n, days) | DOCTRINE, a skill, Claude memory |
| What one trade taught | the trade's `lesson` at the exit | a learning, until it repeats |
| What the next session must check | `log_decision session_summary`, after "Next:" | Claude memory |
| Your view of one coin now | `update_coin_read` (expires in 24h) | a learning |
| The steps for a setup or an event | the skill (rare cases in a reference file beside it) | CLAUDE.md |
| How a part of the system works, a study's write-up | `docs/` | CLAUDE.md, the brief |
| A bug and its fix | the code and a test | a learning, unless it changes how we trade |
| How the owner wants us to work | `CLAUDE.md` section 6 | Claude memory alone (other machines and sessions don't see it) |
| A fact about this machine or the owner that doesn't belong in git | Claude memory | the repo |
| Working notes for this session | the scratchpad | anywhere that outlives the session |

## How a lesson becomes a rule, and stops being one
1. **Observation:** an exit lesson or a logged decision. One trade; it returns in `read_coin`'s `past_trades`.
2. **Hypothesis:** `add_learning` once it looks like a pattern. `similar` in the result shows the learnings that
   already say it: add your evidence to one of those (`update_learning`) instead of writing a second one.
3. **Supported:** a real sample (n of 30 or more independent episodes, stated with days and where the sample lives).
   It enters the brief's rulebook by itself.
4. **In the doctrine or a skill:** `research-review` writes the rule into `docs/DOCTRINE.md` or the setup's skill with
   its id in brackets. Batch these changes in the weekly review, not after each trade.
5. **In code:** a rule that must hold every time becomes a veto, a parameter or a guardian check, with a test.
6. **Out:** rejected, superseded (`superseded_by`), past `valid_until`, or cited by losing trades (the brief flags it).
   The row stays in the journal; the rule leaves the doctrine and the skill.

The brief checks step 4 at every session start: a learning that DOCTRINE or a skill cites but the journal no longer
holds as settled is named under "learnings to re-check" (`doctrine_check.py`).

## Before adding a line to an always-loaded file
- Would a session make a mistake without it? If not, it goes in layer 3 or 4.
- Is it already said somewhere? Then point to that place.
- Does it carry a number that will change (a win rate, a count)? Cite the learning; don't copy the number.
