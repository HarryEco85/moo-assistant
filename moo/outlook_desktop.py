"""Outlook via the classic Outlook app on this PC (no IT setup needed).

Moo talks to Outlook the way add-ins do, using the account you're already
signed into. She reads mail and calendar and can save (and open) drafts for
you to check. She never sends, deletes, moves, or marks anything as read.
Needs classic Outlook (not "new Outlook"); Outlook opens itself if closed.
"""
import html
import logging
import re
from datetime import datetime, timedelta

from .outlook import OutlookNotConfigured, UNTRUSTED_NOTE

log = logging.getLogger("moo")

INBOX, SENT, CALENDAR = 6, 5, 9
MAIL_CLASS, MEETING_CLASSES = 43, {53, 54, 55, 56, 57}


def _naive(dt):
    """Outlook hands back local times; drop the tz marker pywin32 adds."""
    try:
        return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    except Exception:
        return None


class _Session:
    """COM must be set up per thread; use as `with _Session() as ns:`."""

    def __enter__(self):
        try:
            import pythoncom
            import win32com.client
        except ImportError:
            raise OutlookNotConfigured()
        pythoncom.CoInitialize()
        self._pc = pythoncom
        try:
            self.app = win32com.client.Dispatch("Outlook.Application")
            self.ns = self.app.GetNamespace("MAPI")
        except Exception as e:
            pythoncom.CoUninitialize()
            log.warning("Outlook desktop unavailable: %s", e)
            raise RuntimeError("I couldn't open classic Outlook on this PC. Check it's installed and that "
                               "'New Outlook' is switched off.")
        return self

    def __exit__(self, *exc):
        self._pc.CoUninitialize()


def _sender(item) -> str:
    name = getattr(item, "SenderName", "") or ""
    addr = ""
    try:
        if getattr(item, "SenderEmailType", "") == "EX":
            addr = item.Sender.GetExchangeUser().PrimarySmtpAddress
        else:
            addr = item.SenderEmailAddress
    except Exception:
        pass
    return f"{name} <{addr}>" if addr else name


def _line(item, folder_label="") -> str:
    when = _naive(getattr(item, "ReceivedTime", None) or getattr(item, "SentOn", None))
    flags = []
    if getattr(item, "UnRead", False):
        flags.append("UNREAD")
    if getattr(item, "Importance", 1) == 2:
        flags.append("HIGH IMPORTANCE")
    try:
        if item.Attachments.Count:
            flags.append("attachments")
    except Exception:
        pass
    preview = re.sub(r"\s+", " ", (getattr(item, "Body", "") or ""))[:220]
    stamp = f"{when:%Y-%m-%d %H:%M}" if when else ""
    return (f"- id=od:{item.EntryID} | Outlook{folder_label} | {stamp} | from {_sender(item)} | "
            f"{item.Subject or '(no subject)'}{' | ' + ', '.join(flags) if flags else ''}\n  preview: {preview}")


class OutlookDesktop:
    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        import importlib.util
        return importlib.util.find_spec("win32com") is not None

    # ---------- mail ----------
    def _recent(self, folder, since, limit, unread_only=False, date_field="ReceivedTime"):
        items = folder.Items
        items.Sort(f"[{date_field}]", True)  # newest first
        out, seen = [], 0
        item = items.GetFirst()
        while item is not None and len(out) < limit and seen < 2000:
            seen += 1
            try:
                cls = item.Class
                when = _naive(getattr(item, date_field))
                if when and when < since:
                    break
                if cls == MAIL_CLASS or cls in MEETING_CLASSES:
                    if not unread_only or item.UnRead:
                        out.append(item)
            except Exception:
                pass
            item = items.GetNext()
        return out

    def list_emails(self, folder="inbox", hours=24, unread_only=False, limit=25) -> str:
        hours = max(1, min(int(hours), 24 * 31))
        limit = max(1, min(int(limit), 50))
        since = datetime.now() - timedelta(hours=hours)
        with _Session() as s:
            if folder == "sent":
                items = self._recent(s.ns.GetDefaultFolder(SENT), since, limit, unread_only, "SentOn")
            else:
                items = self._recent(s.ns.GetDefaultFolder(INBOX), since, limit, unread_only)
            lines = [_line(i) for i in items]
        if not lines:
            return f"No Outlook emails in {folder} in the last {hours} hours."
        return UNTRUSTED_NOTE + "\n" + "\n".join(lines)

    @staticmethod
    def _dasl(query):
        words = [w.replace("'", "''") for w in re.findall(r"[\w@.\-]+", query)][:6]
        if not words:
            return None
        fields = ['"urn:schemas:httpmail:subject"', '"urn:schemas:httpmail:fromname"',
                  '"urn:schemas:httpmail:fromemail"', '"urn:schemas:httpmail:textdescription"',
                  '"urn:schemas:httpmail:displayto"']
        parts = ["(" + " OR ".join(f"{f} LIKE '%{w}%'" for f in fields) + ")" for w in words]
        return "@SQL=" + " AND ".join(parts)

    def search_emails(self, query: str, limit=15) -> str:
        flt = self._dasl(query)
        if not flt:
            return "Give me some words to search for."
        limit = max(1, min(int(limit), 25))
        found = []
        with _Session() as s:
            inbox = s.ns.GetDefaultFolder(INBOX)
            folders = [(inbox, ""), (s.ns.GetDefaultFolder(SENT), " sent")]
            try:  # filed mail: one level of folders under the inbox
                for i in range(1, min(inbox.Folders.Count, 40) + 1):
                    sub = inbox.Folders.Item(i)
                    folders.append((sub, f" / {sub.Name}"))
            except Exception:
                pass
            for folder, label in folders:
                try:
                    items = folder.Items.Restrict(flt)
                    items.Sort("[ReceivedTime]", True)
                    item, n = items.GetFirst(), 0
                    while item is not None and n < limit:
                        found.append((_naive(getattr(item, "ReceivedTime", None)) or datetime.min, _line(item, label)))
                        n += 1
                        item = items.GetNext()
                except Exception as e:
                    log.info("Search in %s failed: %s", label or "inbox", e)
        if not found:
            return f"No Outlook emails found for '{query}'."
        found.sort(key=lambda x: x[0], reverse=True)
        return UNTRUSTED_NOTE + "\n" + "\n".join(line for _, line in found[:limit])

    def read_email(self, message_id: str) -> str:
        entry = message_id.split(":", 1)[-1]
        with _Session() as s:
            try:
                m = s.ns.GetItemFromID(entry)
            except Exception:
                return "Couldn't find that Outlook email."
            atts = []
            try:
                atts = [m.Attachments.Item(i).FileName for i in range(1, m.Attachments.Count + 1)]
            except Exception:
                pass
            body = re.sub(r"\n{3,}", "\n\n", (m.Body or "").replace("\r", ""))[:6000]
            when = _naive(getattr(m, "ReceivedTime", None))
            stamp = f"{when:%Y-%m-%d %H:%M}" if when else ""
            return (f"{UNTRUSTED_NOTE}\nSubject: {m.Subject}\nFrom: {_sender(m)}\nTo: {getattr(m, 'To', '')}\n"
                    f"Cc: {getattr(m, 'CC', '')}\nReceived: {stamp}\n"
                    f"Attachments: {', '.join(atts) if atts else 'none'}\n\n{body}")

    # ---------- calendar ----------
    def calendar_events(self, start: datetime, end: datetime) -> list:
        events = []
        with _Session() as s:
            items = s.ns.GetDefaultFolder(CALENDAR).Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            try:  # narrow down first (date format follows Windows' regional settings)
                fmt = "%d/%m/%Y %H:%M" if self._day_first() else "%m/%d/%Y %H:%M"
                items = items.Restrict(f"[Start] < '{end.strftime(fmt)}' AND [End] > '{start.strftime(fmt)}'")
            except Exception as e:
                log.info("Calendar restrict failed (%s); scanning instead", e)
            item, seen = items.GetFirst(), 0
            while item is not None and seen < 3000:
                seen += 1
                st, en = _naive(item.Start), _naive(item.End)
                if st and st >= end and seen > 1:
                    break
                if st and en and en > start and st < end:
                    events.append({"start": st, "end": en, "title": item.Subject or "", "all_day": bool(item.AllDayEvent),
                                   "location": getattr(item, "Location", "") or "",
                                   "organiser": getattr(item, "Organizer", "") or "", "source": "Outlook"})
                item = items.GetNext()
        return sorted(events, key=lambda e: e["start"])

    def calendar(self, start_date: str, days=1) -> str:
        from .outlook import events_text, day_range
        start, end, days = day_range(start_date, days)
        return events_text(self.calendar_events(start, end), "Outlook calendar", start, days)

    @staticmethod
    def _day_first() -> bool:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(80)
            ctypes.windll.kernel32.GetLocaleInfoEx(None, 0x1F, buf, 80)  # LOCALE_SSHORTDATE
            return buf.value.lower().startswith("d")
        except Exception:
            return True

    # ---------- drafts ----------
    def create_draft(self, to="", subject="", body="", cc="", reply_to_message_id="") -> str:
        text_html = ("<div style='font-family:Calibri,Arial,sans-serif;font-size:11pt'>" +
                     html.escape(body, quote=False).replace("\n", "<br>") + "</div>")
        with _Session() as s:
            if reply_to_message_id:
                orig = s.ns.GetItemFromID(reply_to_message_id.split(":", 1)[-1])
                draft = orig.Reply()
                draft.HTMLBody = text_html + "<br>" + draft.HTMLBody  # keeps the thread underneath
            else:
                draft = s.app.CreateItem(0)
                draft.To = to
                if cc:
                    draft.CC = cc
                draft.Subject = subject
                draft.HTMLBody = text_html + "<br>" + (draft.HTMLBody or "")
            if to and reply_to_message_id:
                draft.To = to
            draft.Save()  # goes into Drafts; never sent
            subj = draft.Subject
            if self.settings.get("open_drafts", True):
                try:
                    draft.Display(False)  # open it on screen to check and send
                except Exception:
                    pass
        return f"Draft saved in Outlook Drafts: '{subj}'" + (" and opened on screen" if self.settings.get(
            "open_drafts", True) else "") + ". Not sent."

    def token(self, interactive=True):  # same interface as the online connector
        return self.test()

    def test(self) -> str:
        with _Session() as s:
            inbox = s.ns.GetDefaultFolder(INBOX)
            return f"Connected to Outlook on this PC ({inbox.Items.Count} emails in the inbox)."
