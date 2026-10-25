"""Navigating the computer: finding things, opening apps, and handing coding work to Claude Code / Grok Build.

Baymon doesn't write code itself. It delegates to a coding agent through that agent's
non-interactive mode (commands run without a keyboard attached), then reports the result.
say(...) is spoken; show=... is the on-screen version when it differs.
"""

from distill.build import act, call, ex, result, say, user

ENV = "Environment:\n- cwd: {cwd}\n- os: Linux 6.8\n- shell: bash"


def env(cwd: str = "/home/user") -> str:
    return ENV.format(cwd=cwd)


EXAMPLES = [
    ex("pc-001", "computer",
       user("open claude code in my baymon project and ask it why the nudge tests fail"),
       act(call("run_command", command="cd ~/Documents/Baymon && claude -p \"Run the nudge tests and explain briefly why "
                                       "any fail. Don't change files.\"", timeout=600)),
       result("$ cd ~/Documents/Baymon && claude -p \"...\"\nRan `pytest tests/test_nudges.py`: 1 failed.\n"
              "test_snooze_resurfaces_without_model fails because `feedback()` stores `snooze_until` in local time but "
              "`check()` compares it to UTC, so the snooze ends 5.5 hours late in IST.\n[exit code 0]"),
       say("One nudge test fails: snoozes are saved in local time but compared against UTC, so in India they run five "
           "and a half hours long. Want me to have Claude Code fix it?",
           show="Claude Code says one test fails, `test_snooze_resurfaces_without_model`: `feedback()` saves "
                "`snooze_until` in local time, but `check()` compares it against UTC, so in IST a snooze lasts 5.5 hours "
                "too long.\n\nI asked it not to change anything. Want me to tell it to fix that and re-run the tests?"),
       tools="cli", context=env()),

    ex("pc-002", "computer",
       user("yes tell it to fix it"),
       act(call("run_command", command="cd ~/Documents/Baymon && claude -p --continue \"Fix that timezone bug, then run "
                                       "the full test suite and summarise what changed.\"", timeout=900)),
       result("$ cd ~/Documents/Baymon && claude -p --continue \"...\"\nChanged src/baymon/nudges.py: snooze_until is now "
              "stored in UTC.\nRan pytest: 99 passed.\n[exit code 0]"),
       say("Fixed, and all 99 tests pass. Nothing's committed yet, so have a look at the diff when you're ready.",
           show="Fixed: Claude Code now stores `snooze_until` in UTC in `src/baymon/nudges.py`, and the full suite "
                "passes (99 tests). Nothing's committed; have a look at the diff when you're ready."),
       tools="cli", context=env("/home/user/Documents/Baymon")),

    ex("pc-003", "computer",
       user("use grok build to add dark mode to my portfolio site"),
       act(call("run_command", command="grok --help")),
       result("$ grok --help\nUsage: grok [options] [prompt]\n\nOptions:\n  -p, --prompt <text>   run one task non-interactively "
              "and exit\n  -d, --directory <dir>  working directory\n  -h, --help\n[exit code 0]"),
       act(call("run_command", command="grok -d ~/code/portfolio -p \"Add a dark mode toggle that follows the system "
                                       "setting by default. Keep the existing design. Summarise the files you changed.\"",
                timeout=900)),
       result("$ grok -d ~/code/portfolio -p \"...\"\nAdded dark mode via CSS variables and a toggle in the navbar.\n"
              "Changed: src/styles/theme.css, src/components/Navbar.tsx, src/hooks/useTheme.ts\n[exit code 0]"),
       say("Grok Build added dark mode: it follows your system setting, with a toggle in the navbar. Want me to start "
           "the dev server so you can see it?",
           show="Grok Build added dark mode. It follows your system setting by default, with a toggle in the navbar.\n\n"
                "Changed: `src/styles/theme.css`, `src/components/Navbar.tsx`, and a new `src/hooks/useTheme.ts`.\n\n"
                "Want me to start the dev server so you can see it?"),
       tools="cli", context=env()),

    ex("pc-004", "computer",
       user("where's that pdf i downloaded about the internship offer"),
       act(call("glob", pattern="**/*.pdf", path="~/Downloads")),
       result("Downloads/Offer_Letter_Razorpay_2026.pdf\nDownloads/bank_statement_sep.pdf\nDownloads/ISE_timetable.pdf"),
       say("It's the Razorpay offer letter in your Downloads. Want me to open it, or pull out the stipend and start date?",
           show="`~/Downloads/Offer_Letter_Razorpay_2026.pdf`. Want me to open it, or pull out the stipend and start date?"),
       tools="cli", context=env()),

    ex("pc-005", "computer",
       user("open it"),
       act(call("run_command", command="xdg-open ~/Downloads/Offer_Letter_Razorpay_2026.pdf")),
       result("$ xdg-open ~/Downloads/Offer_Letter_Razorpay_2026.pdf\n[exit code 0]"),
       say("Opened."),
       tools="cli", context=env()),

    ex("pc-006", "computer",
       user("my laptop is super slow"),
       act(call("run_command", command="ps aux --sort=-%cpu | head -6 && free -h && df -h / | tail -1")),
       result("$ ps aux --sort=-%cpu | head -6 && free -h && df -h / | tail -1\nUSER  PID %CPU %MEM COMMAND\n"
              "user 4121 187 6.2 node /usr/bin/webpack --watch\nuser 2210 38.1 12.4 /opt/google/chrome/chrome\n"
              "user 3302 9.0 3.1 code\n               total   used   free\nMem:           15Gi   14Gi  320Mi\n"
              "/dev/nvme0n1p2  234G  229G  5.0G  98% /\n[exit code 0]"),
       say("Three culprits: a forgotten webpack process eating two cores, memory nearly full, and a disk at ninety-eight "
           "percent. Should I kill webpack and find what's filling the disk?",
           show="Three culprits:\n\n- **A `webpack --watch` process** is using almost 2 cores (PID 4121). Probably a dev "
                "server you forgot about.\n- **Memory is nearly full:** 14 of 15 GB, with Chrome the biggest user.\n"
                "- **The disk is 98% full,** which slows everything down.\n\nShould I kill the webpack process and find "
                "what's taking the disk space?"),
       tools="cli", context=env()),

    ex("pc-007", "computer",
       user("yes kill it and find the big stuff"),
       act(call("run_command", command="kill 4121"),
           call("run_command", command="du -h --max-depth=1 ~ 2>/dev/null | sort -rh | head -6")),
       result("$ kill 4121\n[exit code 0]",
              "$ du -h --max-depth=1 ~ 2>/dev/null | sort -rh | head -6\n181G\t/home/user\n96G\t/home/user/.cache\n"
              "41G\t/home/user/Videos\n22G\t/home/user/code\n9.8G\t/home/user/Downloads\n[exit code 0]"),
       say("Killed it. The big one is your cache folder at ninety-six gigabytes, usually safe to clear; should I look "
           "inside before deleting anything?",
           show="Killed webpack.\n\nThe big one is **`~/.cache`: 96 GB.** That's usually package and browser caches, "
                "and safe to clear. Videos (41 GB) is next.\n\nShould I look inside `.cache` first? I won't delete "
                "anything without telling you."),
       tools="cli", context=env()),

    ex("pc-008", "computer",
       user("start the baymon api locally"),
       act(call("run_command", command="cd ~/Documents/Baymon && (nohup uv run baymon-api > /tmp/baymon-api.log 2>&1 &) "
                                       "&& sleep 4 && curl -s localhost:8000/health")),
       result("$ cd ~/Documents/Baymon && ... && curl -s localhost:8000/health\n{\"status\":\"ok\"}\n[exit code 0]"),
       say("It's up on port 8000 and healthy.",
           show="It's up on http://localhost:8000 and healthy. Logs are going to `/tmp/baymon-api.log`."),
       tools="cli", context=env()),

    ex("pc-009", "computer",
       user("ask claude code to refactor the whole repo to typescript"),
       say("That's a big one, so two quick checks before I hand it over. They're on screen.",
           show="That's a big one: a whole-repo rewrite, and hard to review. Before I send it, two checks:\n\n"
                "1. Is everything committed, so it's easy to undo? I can check `git status`.\n"
                "2. In one go, or module by module, starting with the API layer? Module by module is much easier "
                "to review.\n\nTell me which, and I'll hand it over.")),

    ex("pc-010", "computer",
       user("is claude code even installed"),
       act(call("run_command", command="command -v claude && claude --version")),
       result("$ command -v claude && claude --version\n[exit code 1]"),
       say("No, it isn't installed. Want me to install it?",
           show="No, `claude` isn't on your PATH. It installs with `npm install -g @anthropic-ai/claude-code`; want me "
                "to run that?"),
       tools="cli", context=env()),
]
