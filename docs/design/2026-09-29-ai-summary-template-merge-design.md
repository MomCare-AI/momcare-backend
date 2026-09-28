# AI Summary Template Merge + AI-Assisted Authoring — Design Note

**Status:** Agreed in chat 2026-09-29, implemented directly (same "skip the written-spec round"
choice as the templates feature itself the day before -- this note is the record of what was
agreed, not a review gate).

## What changed, and why

`AIInstructionPreset` (free-text extra instructions, two tiers, stacking platform + org together)
and `AISummaryTemplate` (structured field layout, two tiers, non-stacking) were built as two
separate systems one day apart. After using both, the user concluded they were one setting split
in two for no real reason -- a hospital picking a layout and a hospital adding a wording note are
the same act ("configure how our summaries read"), not two.

**Resolution: `AIInstructionPreset` is retired. `AISummaryTemplate` gains one new field,
`extra_instructions` (blank-allowed text), carrying what a preset used to hold.** One record per
scope now carries both the layout and the wording, created/activated/deactivated together as a
single unit -- the exact lifecycle `AISummaryTemplate` already had.

**Behavior consequence, decided explicitly:** the merged field is **non-stacking**, matching how
templates already worked -- not stacking, the way presets used to. A hospital's own active
template (layout *and* wording together) wins outright over the platform's; the platform's applies
only when the hospital has none of its own. The platform can no longer broadcast a wording note to
every hospital regardless of what that hospital has configured -- judged an acceptable loss, since
that broadcast case never came up as a real need, and consistency (one merge behavior, not "layout
doesn't stack but wording does") was worth more.

**Data safety on merge:** since this is a live system, a data migration copies any currently-active
preset's content into the corresponding scope's template before the old table is dropped --
creating a template (default layout, since none may exist yet) if the scope had a preset but no
template. No hospital's already-configured wording is silently lost by this change.

## AI-assisted template authoring (new)

Building a 17-field arrangement by hand is small enough to not need AI help -- floated, then
explicitly rejected by the user once framed that way. What *is* kept: a single **propose/preview**
loop, at both tiers, where an admin describes what they want in plain English (layout, wording, or
both, in one message) and the AI proposes a full candidate -- not manual construction:

- The AI is prompted with the exact fixed vocabulary and told every field must appear exactly
  once, nothing invented, nothing omitted -- the same rule `validate_template_sections()` already
  enforces for hand-built templates, run identically against the AI's own proposal. An invalid
  proposal is retried automatically (bounded) before surfacing a clean error.
- A preview is generated the same way a real summary would be, using **fixed sample data, never a
  real patient**, at both tiers -- keeps platform and organization previews identical in mechanism,
  and keeps no real PHI in what is fundamentally a design/testing tool. Respects the same word cap
  a real summary would.
- The admin can ask again for a different candidate as many times as they like -- each call is
  independent and stateless; nothing is saved until they explicitly save.
- Saving/activating a chosen candidate uses the endpoints that already existed for templates --
  no new save mechanism, the propose/preview step only ever produces a *draft* the existing create
  endpoint accepts.

**What was explicitly considered and rejected:** a persistent, general-purpose AI chatbot as the
mechanism for this. A chatbot answers a question in the moment; it does not explain how a setting
keeps applying automatically to every future summary, for every patient, with nobody present to
say it again. The propose/preview loop is authoring-time only -- once saved, the template is
replayed silently by ordinary code, the same as it already was before AI-assisted authoring
existed.

## Not changed by this note

Everything else about `AISummaryTemplate` from
`docs/design/2026-09-28-ai-summary-templates-design.md` stands: the fixed 17-field vocabulary
(no new fields -- "hospital name" was floated as an example and explicitly declined), the
org-then-platform-then-built-in-default precedence for which template applies, the RLS shape, and
the immutable/never-deleted/at-most-one-active-per-scope lifecycle.
