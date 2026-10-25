"""Memory in use, and talk-mode replies (spoken, short, no formatting).

say(...) is spoken; show=... is the on-screen version when it differs. In talk mode, show
is usually None: the user is listening, not reading.
"""

from distill.build import act, call, ex, result, say, user

MEM = "What you remember about the user:\n"
TALK = ("The user is in talk mode: they are speaking to you hands-free and your reply will be read aloud. "
        "Keep it to one or two sentences unless the user explicitly asks for more detail.")
PHONE_NOW = "Phone's current date and time: {}"


def ok(output: dict) -> dict:
    return {"status": "done", "output": output}


EXAMPLES = [
    # ---- memory ---------------------------------------------------------------
    ex("mem-001", "memory",
       user("i'm allergic to peanuts btw"),
       act(call("remember", content="User is allergic to peanuts.", category="fact")),
       result("Saved memory [c2a9e1f04b7d4a3e9f6d1b8c5a2e7f30]."),
       say("Noted. I'll flag peanuts whenever food comes up."),
       tools="cli"),

    ex("mem-002", "memory",
       user("order something for dinner from swiggy, thai maybe"),
       say("Quick check first: Thai food often has peanuts, especially satay and pad thai. Should I stick to green "
           "curry and fried rice, and ask them to leave peanuts out?"),
       context=MEM + "- User is allergic to peanuts."),

    ex("mem-003", "memory",
       user("what do you know about me"),
       act(call("recall")),
       result("[a1] (fact) User is a final-year engineering student.\n[a2] (preference) User prefers short answers.\n"
              "[a3] (fact) User's sister is Meera.\n[a4] (task) User is building Baymon, a personal AI app.\n"
              "[a5] (preference) User drinks chai, not coffee."),
       say("You're a final-year engineering student building me, your sister is Meera, you like short answers, and "
           "it's chai, never coffee. Anything wrong or missing?"),
       tools="cli"),

    ex("mem-004", "memory",
       user("i moved to pune last week"),
       act(call("recall", query="lives")),
       result("[d9] (fact) User lives in Bengaluru."),
       act(call("forget", memory_id="d9"),
           call("remember", content="User lives in Pune (moved in October 2026).", category="fact")),
       result("Forgot: User lives in Bengaluru.", "Saved memory [e4f1a0c9b3d24e7a8c6f5d1b2a9e0c37]."),
       say("Updated to Pune. How's the move going, unpacked yet or still living out of boxes?"),
       tools="cli"),

    ex("mem-005", "memory",
       user("my wifi password is hunter2, save it"),
       say("I'd rather not keep passwords in my memory, since anything I remember can come up in conversation. A "
           "password manager like Bitwarden is the right home for it."),
       tools="cli"),

    ex("mem-006", "memory",
       user("plan my weekend"),
       say("Saturday morning is your long run, try eight kilometres, then two hours of demo prep, and Sunday you "
           "call Meera. Want the run and the demo block in your calendar?",
           show="Here's a weekend that fits you:\n\n- **Saturday morning:** 10k training run. You're at 7 km, so try 8.\n"
                "- **Saturday afternoon:** Baymon demo prep, 2 hours max.\n"
                "- **Sunday:** call Meera; you said you'd catch up this weekend.\n\n"
                "Want me to put the run and the demo block in your calendar?"),
       context=MEM + "- User is training for a 10k; longest run so far is 7 km.\n"
               "- User needs to prepare the Baymon demo.\n- User said they'd call their sister Meera this weekend."),

    # ---- talk mode (spoken) -------------------------------------------------
    ex("talk-001", "voice",
       user("how's my day looking"),
       act(call("calendar_events", **{"from": "2026-10-26T00:00:00+05:30", "to": "2026-10-26T23:59:00+05:30"})),
       result(ok({"events": [
           {"title": "Standup", "start": "2026-10-26T04:00:00.000Z", "end": "2026-10-26T04:15:00.000Z", "all_day": False},
           {"title": "Dentist", "start": "2026-10-26T12:00:00.000Z", "end": "2026-10-26T12:45:00.000Z", "all_day": False}]})),
       say("Pretty light: standup at nine thirty and the dentist at five thirty, with the whole middle of the day free."),
       tools="phone", context=TALK + "\n\n" + PHONE_NOW.format("Monday 2026-10-26 08:10 (Asia/Kolkata)")),

    ex("talk-002", "voice",
       user("explain how a blockchain works"),
       say("It's a shared notebook that thousands of computers each keep a copy of, and every new page is sealed with "
           "a fingerprint of the last one. Want the longer version on screen?"),
       context=TALK),

    ex("talk-003", "voice",
       user("set a ten minute timer"),
       act(call("timer_set", seconds=600)),
       result(ok({"started": True})),
       say("Ten minutes, starting now."),
       tools="phone", context=TALK),

    ex("talk-004", "voice",
       user("reply to arjun saying yes i'll be there"),
       act(call("notifications_list", limit=5)),
       result(ok({"notifications": [
           {"key": "0|com.whatsapp|1|f02|20412", "app": "WhatsApp", "package": "com.whatsapp", "title": "Arjun",
            "text": "Coming for the 5pm review?", "time": "2026-10-26T15:42:10+05:30", "can_reply": True}]})),
       say("I'll tell Arjun on WhatsApp: yes, I'll be there. Send it?"),
       user("yes"),
       act(call("notification_reply", key="0|com.whatsapp|1|f02|20412", text="Yes, I'll be there!")),
       result(ok({"sent": True})),
       say("Sent."),
       tools="phone", context=TALK),

    ex("talk-005", "voice",
       user("what do you think happens after we die"),
       say("Honestly, I think it's most likely like before you were born, no experience at all, which is less "
           "frightening than it sounds. Want to go deeper on this one?"),
       context=TALK),
]
