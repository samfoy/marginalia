local _ = require("gettext")
return {
    name        = "piread",
    fullname    = _("Pi reading assistant"),
    description = _([[AI-powered reading assistant backed by your local Mac. X-Ray entity graphs (characters, locations, references, timeline) from your Calibre library, with position-bounded retrieval (RAG) for grounded, spoiler-safe answers. 'Now Reading' dashboard, AI Wiki deep-dives, Section X-Ray, recap-on-resume, jump-to-mentions. Requires piread-bridge on your Mac.]]),
    version     = "0.3.0",
}
