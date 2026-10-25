# Baymon distillation data

Training conversations for fine-tuning **Gemma 4 E4B** into Baymon. Each one shows Baymon behaving exactly as
intended: its personality, real thinking, correct tool calls, and honesty.

## The plan

1. **Model:** Gemma 4 E4B. ✅
2. **Data:** these conversations. ← we are here
3. **Train:** LoRA fine-tune with Unsloth on the generated `data/train.jsonl`.
4. **Evaluate:** score it on `data/eval.jsonl` before it replaces anything.

## Files

| File | What it is |
|---|---|
| `persona.md` | Baymon's voice: the target personality. Every example has to match it. |
| `examples/*.py` | The conversations, one file per category, written to be readable. |
| `build.py` | Checks every example and writes the training files. |
| `data/train.jsonl`, `data/eval.jsonl` | Generated output (10 % held out for evaluation). |

## Categories

| Category | Teaches |
|---|---|
| `persona` | Everyday conversation: warmth, wit, pushback, hard moments |
| `philosophy` | Genuine positions with arguments; admits uncertainty |
| `computer` | Navigating the computer and handing coding to Claude Code or Grok Build |
| `phone` | Phone control with the phone tools: direct tools first, read before tapping, confirm before messaging |
| `honesty` | Reporting failures truthfully, not inventing facts, using memory correctly |

## Build

```bash
uv run python -m distill.build            # check all examples, write data/*.jsonl
uv run python -m distill.build --show 3   # also print 3 random examples readably
```

The checker rejects:
- tools that don't exist, or arguments that don't match the real tool definitions;
- tool results without a matching call, or calls left unanswered;
- empty replies;
- conversations that don't end with Baymon speaking.

## Format

Each line is one conversation:
- `messages`: system, user, assistant (with `tool_calls`), tool, and so on;
- `tools`: the exact tool definitions Baymon had available in that conversation.

This is the format Unsloth's chat-template training expects for tool-calling models.
