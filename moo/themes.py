"""Colours shared by the window and settings."""

BG = "#0B0D12"
SURFACE = "#151922"
SURFACE_HI = "#1C2130"
TEXT = "#E8ECF3"
MUTED = "#8A93A6"
FAINT = "#4A5263"
FONT = "Segoe UI"

# Each theme: orb colour pair per state
THEMES = {
    "Ocean": {"idle": ("#5B8CFF", "#36E0C8"), "listening": ("#36E0C8", "#9AF6FF"),
              "thinking": ("#8B5CF6", "#5B8CFF"), "speaking": ("#5B8CFF", "#36E0C8")},
    "Aurora": {"idle": ("#22C3A6", "#A3F7BF"), "listening": ("#7CFFCB", "#D4FFE6"),
               "thinking": ("#3B82F6", "#22C3A6"), "speaking": ("#22C3A6", "#7CFFCB")},
    "Violet": {"idle": ("#8B5CF6", "#EC4899"), "listening": ("#C084FC", "#F5D0FE"),
               "thinking": ("#6366F1", "#8B5CF6"), "speaking": ("#8B5CF6", "#EC4899")},
    "Ember": {"idle": ("#FF7A45", "#FFB547"), "listening": ("#FFB547", "#FFE3A3"),
              "thinking": ("#FF4D6D", "#FF7A45"), "speaking": ("#FF7A45", "#FFB547")},
    "Arctic": {"idle": ("#7DD3FC", "#E0F2FE"), "listening": ("#BAE6FD", "#FFFFFF"),
               "thinking": ("#60A5FA", "#7DD3FC"), "speaking": ("#7DD3FC", "#E0F2FE")},
    "Graphite": {"idle": ("#8A93A6", "#E8ECF3"), "listening": ("#C9CFDA", "#FFFFFF"),
                 "thinking": ("#5B6478", "#A6AEBD"), "speaking": ("#8A93A6", "#E8ECF3")},
}
OFFLINE = ("#3A4150", "#596277")

TEXT_SIZES = {"Small": (10, 9), "Medium": (12, 11), "Large": (14, 13)}  # Moo's text, your text


def palette(settings):
    return THEMES.get(settings.get("theme", "Ocean"), THEMES["Ocean"])


def accent(settings):
    return palette(settings)["idle"][0]
