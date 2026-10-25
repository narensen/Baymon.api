"""The phone's tool catalog, exactly as the app registers it (src/lib/tools/catalog.ts in the app repo).

Names, descriptions and parameters are copied verbatim so the model trains on the same
definitions it sees in production. If the app's catalog changes, update this file.

OUTPUTS documents what each tool returns. The backend wraps every phone result as
{"status": "done", "output": <output>} or {"status": "failed", "error": {"code", "message"}}.
"""

from typing import Any

_EL = {"type": "integer", "minimum": 1, "description": "Element number from the latest screen_read."}
_VIS = {"type": "string", "description": "Alternatively, the element’s visible text or label."}
_S = {"type": "string"}
_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_SETTINGS = ["wifi", "internet", "bluetooth", "volume", "sound", "display", "battery", "location", "nfc", "airplane",
             "dnd", "notifications", "date", "storage", "apps", "security", "accessibility"]


def _t(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


PHONE_TOOLS: list[dict[str, Any]] = [
    # Screen
    _t("screen_read", "Read what is on the phone screen right now, as a numbered list of elements: text, buttons, fields "
       "(with their current value), switches and lists. Read before acting, and read again after any action that changes "
       "the screen. Element numbers are only valid until the next read."),
    _t("screen_read_all", "Read a long screen in full (an article, a long chat, terms and conditions): collects all the text "
       "while scrolling to the end. Use for \"summarise this\" / \"what does this say\". With app (a name or package), that app "
       "is brought to the front first and Baymon comes back afterwards; when the user is in Baymon, it reads the app they came "
       "from (screen_read shows it as previous_app).",
       {"app": {"type": "string", "description": "App to read (name or package). Omit to read what is on screen, or the "
                                                  "previous app if Baymon is on screen."},
        "pages": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8, "description": "Most screenfuls to scroll through."}}),
    _t("screen_tap", "Tap an element: preferably by its number from the latest screen_read, otherwise by its visible text.",
       {"element": _EL, "text": _VIS}),
    _t("screen_long_press", "Long-press an element (opens context menus, selects messages, etc.).", {"element": _EL, "text": _VIS}),
    _t("screen_type", "Type into a text field, replacing what is in it. Give the field’s element number, or omit it to use the "
       "focused field. Set submit to press the keyboard’s Enter/Search/Send key afterwards.",
       {"text": {"type": "string", "description": "The full text the field should contain."}, "element": _EL,
        "submit": {"type": "boolean", "description": "Press Enter/Send after typing.", "default": False}}, ["text"]),
    _t("screen_scroll", "Scroll the screen or a specific list. \"down\" reveals content further down.",
       {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "element": _EL}, ["direction"]),
    _t("screen_navigate", "System navigation: back, home, recents (app switcher), notifications (pull down the shade), "
       "quick_settings, lock (lock the phone), screenshot.",
       {"action": {"type": "string", "enum": ["back", "home", "recents", "notifications", "quick_settings", "lock", "screenshot"]}},
       ["action"]),
    _t("screen_wait_for", "Wait until some text appears on screen, e.g. after opening an app, tapping Install, or sending. "
       "Returns whether it appeared in time.",
       {"text": _S, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}}, ["text"]),
    # Apps
    _t("apps_list", "List the installed apps (name and package name)."),
    _t("app_open", "Open an installed app by its name as shown on the home screen (e.g. \"Instagram\") or its package name.",
       {"app": _S}, ["app"]),
    _t("app_install", "Open the Play Store for an app (by name, or by package name for an exact page). Then read the screen, "
       "open the right result if needed, and tap Install.", {"app": _S}, ["app"]),
    _t("app_uninstall", "Show the system uninstall dialog for an app (package name from apps_list). Then confirm it on screen.",
       {"package": _S}, ["package"]),
    _t("app_info", "Open an app’s system info page: permissions, notifications, storage and cache, force stop.",
       {"package": _S}, ["package"]),
    # Communication
    _t("contacts_search", "Find people in the phone’s contacts by name. Returns phone numbers and emails.",
       {"query": _S, "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}}, ["query"]),
    _t("phone_call", "Open the dialer with a number filled in (use contacts_search for names). Then tap the call button on screen.",
       {"number": _S}, ["number"]),
    _t("sms_compose", "Open the messaging app with a text message filled in. Then tap Send on screen.",
       {"to": {"type": "string", "description": "Phone number."}, "body": _S}, ["to", "body"]),
    _t("email_compose", "Open the email app with a new email filled in. Then tap Send on screen.",
       {"to": {"type": "string", "description": "Comma-separated addresses."}, "subject": _S, "body": _S, "cc": _S}, ["to"]),
    _t("share", "Open the system share sheet with text or a file (uri/path from files_find or photos_recent). Then read the "
       "screen and pick the app and recipient, e.g. WhatsApp → a group → Send.",
       {"text": _S, "file": {"type": "string", "description": "A file uri or path to share."}}),
    # Time
    _t("alarm_set", "Set an alarm in the clock app. 24-hour time; optional repeat days.",
       {"hour": {"type": "integer", "minimum": 0, "maximum": 23}, "minute": {"type": "integer", "minimum": 0, "maximum": 59},
        "label": _S, "days": {"type": "array", "items": {"type": "string", "enum": _DAYS}}}, ["hour", "minute"]),
    _t("timer_set", "Start a countdown timer in the clock app.",
       {"seconds": {"type": "integer", "minimum": 1, "maximum": 86400}, "label": _S}, ["seconds"]),
    _t("reminder_set", "Remind the user with a notification at a specific time.",
       {"text": _S, "at": {"type": "string", "description": "ISO 8601 date-time with timezone offset."}}, ["text", "at"]),
    _t("reminders_list", "List upcoming reminders Baymon has set on this phone."),
    _t("reminder_cancel", "Cancel a reminder by id (from reminders_list).", {"id": _S}, ["id"]),
    _t("calendar_events", "List calendar events between two times (defaults: now to 7 days ahead).",
       {"from": {"type": "string", "description": "ISO 8601."}, "to": {"type": "string", "description": "ISO 8601."}}),
    _t("calendar_add", "Add an event to the phone’s calendar.",
       {"title": _S, "start": {"type": "string", "description": "ISO 8601."},
        "end": {"type": "string", "description": "ISO 8601. Defaults to one hour after start."}, "location": _S, "notes": _S},
       ["title", "start"]),
    # Files
    _t("files_find", "Find files anywhere in the phone’s storage (Downloads, Documents, WhatsApp media…) whose names contain the "
       "query words. Use short distinctive words (\"7th sem\", \"syllabus\"). Returns name, folder, date and a uri to share.",
       {"query": _S, "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}}, ["query"]),
    _t("photos_recent", "The most recent photos, videos or screenshots, newest first.",
       {"kind": {"type": "string", "enum": ["photo", "video", "screenshot"], "default": "photo"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5}}),
    _t("files_read", "Read a file or image: a screenshot, photo, PDF or document (a uri or path from files_find, photos_recent, or "
       "something the user shared). Returns its text, or for images a description plus any text in it. Use for \"remember this "
       "screenshot\", \"what does this PDF say\", tickets, receipts, bookings.",
       {"uri": {"type": "string", "description": "file:// or content:// uri, or an absolute path."},
        "name": {"type": "string", "description": "File name, if known."}}, ["uri"]),
    _t("clipboard_read", "Read the text on the clipboard (only works while Baymon is on screen)."),
    _t("clipboard_write", "Copy text to the clipboard.", {"text": _S}, ["text"]),
    # Web & places
    _t("open_url", "Open a link. App links (YouTube, Instagram, Maps…) open in their app; others in the browser.", {"url": _S}, ["url"]),
    _t("web_search", "Search the web in the phone’s browser.", {"query": _S}, ["query"]),
    _t("maps_directions", "Start directions to a place in Google Maps.",
       {"destination": _S, "mode": {"type": "string", "enum": ["driving", "walking", "bicycling", "transit"], "default": "driving"}},
       ["destination"]),
    _t("maps_search", "Search for places in Google Maps (e.g. \"coffee near me\").", {"query": _S}, ["query"]),
    _t("location_save_place", "Save where the phone is now (or given coordinates) as a named place, e.g. \"Office\" when the user "
       "says \"this is my office\". Saved places make reminders like \"when I get to the office\" work: arriving and leaving are "
       "noticed even in the background.",
       {"name": {"type": "string", "description": "Short name, e.g. \"Office\", \"Home\", \"Gym\"."}, "latitude": {"type": "number"},
        "longitude": {"type": "number"}, "radius_m": {"type": "integer", "minimum": 75, "maximum": 1000, "default": 150}}, ["name"]),
    _t("location_places", "The places the user has saved (names only; arriving/leaving them can trigger reminders)."),
    _t("location_forget_place", "Delete a saved place by name.", {"name": _S}, ["name"]),
    _t("location_current", "The phone’s current location with a street address."),
    # Device
    _t("device_status", "Battery level and charging state, and network connection."),
    _t("flashlight", "Turn the flashlight on or off.", {"on": {"type": "boolean"}}, ["on"]),
    _t("media_control", "Control whatever is playing (music, YouTube, podcasts) and the media volume.",
       {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop", "volume_up", "volume_down", "mute"]}},
       ["action"]),
    _t("brightness_set", "Set the screen brightness (0–100).", {"level": {"type": "integer", "minimum": 0, "maximum": 100}}, ["level"]),
    _t("settings_open", "Open a settings page or quick panel (Wi-Fi, Bluetooth, volume, Do Not Disturb, …). Then read the screen "
       "to toggle things.", {"page": {"type": "string", "enum": _SETTINGS}}, ["page"]),
    # Notifications
    _t("notifications_list", "List the phone’s current notifications (app, title, text, time), newest first. can_reply means "
       "notification_reply works without opening the app.",
       {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}}),
    _t("notification_reply", "Reply to a message notification (key from notifications_list) without opening the app.",
       {"key": _S, "text": _S}, ["key", "text"]),
]

# What each tool returns inside {"status": "done", "output": …} (from the app and its Android module).
OUTPUTS: dict[str, str] = {
    "screen_read": '{"app": "WhatsApp", "package": "com.whatsapp", "elements": 9, "screen": "[1] button \\"Search\\"\\n[2] text '
                   '\\"Chats\\"\\n[3] button \\"Drum, Hi! See you at 5, 4:12 PM\\"\\n[4] field \\"Message\\" (empty) (focused)"} '
                   '(+ "previous_app"/"previous_package" when Baymon itself is on screen). Roles: button, text, field, switch, '
                   'checkbox, list, image. Fields: = "value", (empty) or (password). Flags: (on)/(off), (selected), (scrollable), '
                   '(disabled), (focused). Over 200 elements ends with "… (more elements below; scroll to see them)".',
    "screen_read_all": '{"app", "package", "pages": 6, "complete": true, "text": "…≤ 30,000 chars, no element numbers…"}',
    "screen_tap": '{"tapped": true} or {"tapped": true, "method": "touch"}',
    "screen_long_press": '{"pressed": true}',
    "screen_type": '{"typed": true, "submitted": false}',
    "screen_scroll": '{"scrolled": true}',
    "screen_navigate": '{"done": true}',
    "screen_wait_for": '{"appeared": true} or {"appeared": false}',
    "apps_list": '{"apps": [{"name": "WhatsApp", "package": "com.whatsapp"}, …]}',
    "app_open": '{"name": "WhatsApp", "package": "com.whatsapp"}  (failure: app_not_installed "No installed app matches \\"X\\".")',
    "app_install": '{"opened": "play_store"}',
    "app_uninstall": '{"opened": "uninstall_dialog"}',
    "app_info": '{"opened": "app_info"}',
    "contacts_search": '{"contacts": [{"name": "Priya Sharma", "phones": ["+91 98450 12345"], "emails": []}]}',
    "phone_call": '{"opened": "dialer"}',
    "sms_compose": '{"opened": "messages"}',
    "email_compose": '{"opened": "email"}',
    "share": '{"opened": "share_sheet"}',
    "alarm_set": '{"set": true}',
    "timer_set": '{"started": true}',
    "reminder_set": '{"id": "8f2c…", "at": "2026-10-26T12:30:00.000Z"}  (failure invalid_args "That time has already passed.")',
    "reminders_list": '{"reminders": [{"id", "text", "trigger"}]}',
    "reminder_cancel": '{"cancelled": true}',
    "calendar_events": '{"events": [{"title", "start", "end", "location"?, "all_day": false}]}',
    "calendar_add": '{"id": "412", "calendar": "Personal"}',
    "files_find": '{"files": [{"name", "path", "uri", "size", "modified_at", "mime"}], "matched_all_words": true}',
    "photos_recent": '{"items": [{"name", "path", "uri", "size", "modified_at", "mime"}]}',
    "files_read": '{"attachment_id": "att_…", "name", "kind": "image|pdf|text|file", "text": "…"}',
    "clipboard_read": '{"text": "…"}',
    "clipboard_write": '{"copied": true}',
    "open_url": '{"opened": true}',
    "web_search": '{"opened": true}',
    "maps_directions": '{"opened": "maps"}  (no travel time: read the screen if you need it)',
    "maps_search": '{"opened": "maps"}',
    "location_save_place": '{"saved": "Office", "radius_m": 150}',
    "location_places": '{"places": [{"name": "Office", "radius_m": 150}]}',
    "location_forget_place": '{"forgotten": "Gym"}',
    "location_current": '{"latitude": 12.97, "longitude": 77.64, "address": "…"}',
    "device_status": '{"battery_percent": 64, "charging": false, "network": "WIFI", "online": true}',
    "flashlight": '{"on": true}',
    "media_control": '{"done": true}',
    "brightness_set": '{"level": 40}',
    "settings_open": '{"opened": "dnd"}',
    "notifications_list": '{"notifications": [{"key", "app", "package", "title", "text", "time", "can_reply"}]}  '
                          '(OTP-like codes in text appear as "[code]")',
    "notification_reply": '{"sent": true}  (failure not_found "That notification is gone. List notifications again.")',
}

ERRORS = {
    "permission_missing": ["Screen control is off. Turn on Baymon in Settings → Accessibility.",
                           "Notification access is off. Turn on Baymon in Settings → Notification access.",
                           "All-files access is off. Allow it for Baymon in Settings → Special app access.",
                           "Contacts permission is off. Allow it in Settings → Apps → Baymon → Permissions."],
    "not_found": ["Element 7 isn't on the last screen read. Read the screen again.",
                  "Element 4 is no longer on screen. Read the screen again.",
                  "Nothing on screen says \"Send\". Read the screen again.",
                  "No text field on screen. Read the screen and pass the field's number."],
    "app_not_installed": ["No installed app matches \"Telegram\"."],
    "not_supported": ["This needs the Baymon development build on Android (screen control isn’t available in Expo Go)."],
    "failed": ["Couldn't tap that element."],
}
