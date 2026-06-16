local _ = require("gettext")
return {
    name        = "marginalia",
    fullname    = _("Marginalia"),
    description = _([[AI-powered reading companion backed by a local bridge server. Generates a Book Index (characters, locations, references, timeline) from your Calibre EPUB library, with position-bounded retrieval (RAG) for grounded, spoiler-safe answers across a book series. Includes a 'Now Reading' dashboard, AI Wiki deep-dives, Section Book Index, recap-on-resume, jump-to-mentions, gesture bindings, and offline note saving that syncs to your Obsidian vault. Requires marginalia bridge running on your computer.]]),
    version     = "0.7.0",
}
